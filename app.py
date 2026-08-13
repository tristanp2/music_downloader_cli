#!/usr/bin/env python3
"""
app.py  --  FastAPI webserver for the music downloader.

Owns the ONE Deezer session (+ deemix settings) for the whole system. The CLI
and sync.py post download requests here instead of spinning their own session,
so a shared ARL is never double-logged (see core/server_lock).

Endpoints
---------
  POST /download   {url}           -> starts a job, returns {job_id}
  GET  /jobs/{id}                  -> job status + live progress lines
  GET  /jobs                       -> the whole queue (for the frontend)
  GET  /playlists                  -> filesystem registry (playlist.meta.json)
  GET  /health                     -> Deezer session liveness + trial check
  POST /reload                     -> re-login via ARL (refresh expired token)
  GET  /                           -> serves templates/index.html

Auth: POST /download requires header X-Auth-Token matching the shared secret
(env MUSIC_DOWNLOADER_SERVER_TOKEN or config/settings.conf server_token). GETs are open
(localhost LAN use). Set the token or the endpoint stays unauthenticated.

Run:
  .venv/Scripts/python.exe app.py            # binds via config/settings.conf `bind_host`
  # BIND_HOST is read from config `bind_host` (or env MUSIC_DOWNLOADER_BIND_HOST),
  # defaulting to 0.0.0.0 (all interfaces). Set `bind_host` to your LAN IP to
  # bind that NIC only, keeping the server off the Tailscale virtual interface.
  # Optional arg = port (default 8000), or set env MUSIC_DOWNLOADER_PORT.
"""
import os
import sys
import time
import threading
import uuid
import atexit
import json
import asyncio
import traceback
from pathlib import Path
from collections import defaultdict
from dataclasses import asdict

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles

from core.config import resolve_output_dir, sync_deezer_arl, ARL, read_conf, read_users
from core.deezer import init_deezer
from core.downloader import run_playlist
from core.registry import list_playlists
from core.library import find_existing_track
from core import server_lock
from core import log
from core import attach_uvicorn_loggers
from core.job import Job, JobProgress, TrackState
from core.event_types import JobEventType, DownloadStatus

REPO = Path(__file__).resolve().parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


from contextlib import asynccontextmanager


@asynccontextmanager
async def lifespan(app):
    global DZ, EVENT_LOOP
    EVENT_LOOP = asyncio.get_running_loop()
    attach_uvicorn_loggers()
    if not ARL.is_file():
        log.info("[startup] deezer.arl missing -- /download will 503 until added")
    else:
        arl_text = ARL.read_text(encoding="utf-8").strip()
        sync_deezer_arl()
        try:
            DZ = init_deezer(arl_text)
            log.info("[startup] Deezer session established")
        except Exception as e:
            log.warning("[startup] Deezer login failed: %s", e)
    server_lock.write_lock(PORT)
    try:
        yield
    finally:
        server_lock.clear_lock()


app = FastAPI(title="music-downloader", lifespan=lifespan)

# ---------------------------------------------------------------------------
# server-side state (single process)
# ---------------------------------------------------------------------------
try:
    import deemix.settings as _dms
    SETTINGS = _dms.load(REPO / "config")
except Exception as _e:
    from core import log as _log
    _log.warning("could not load deemix settings: %s", _e)
    SETTINGS = {}

WORK_DIR_BASE = resolve_output_dir()
WORK_DIR_BASE.mkdir(parents=True, exist_ok=True)

def _require_user(request: Request) -> str:
    """Extract ?user= query param. Reject if missing or not in config whitelist."""
    user = request.query_params.get("user")
    if not user:
        raise HTTPException(status_code=400, detail="missing 'user' query parameter")
    allowed = read_users()
    if user not in allowed:
        raise HTTPException(status_code=403, detail="user '{}' not in allowed list ({})".format(user, ",".join(allowed)))
    return user

def _resolve_user_path(user: str, folder: str) -> Path:
    """Safely resolve {WORK_DIR_BASE}/{user}/{folder}."""
    fp = (WORK_DIR_BASE / user / folder).resolve()
    base = str(WORK_DIR_BASE.resolve())
    if not str(fp).startswith(base):
        raise HTTPException(status_code=403, detail="invalid path")
    return fp

DZ = None          # the one Deezer session, set in startup()
DZ_LOCK = threading.Lock()   # serialize Deezer calls (session not thread-safe)
JOBS = {}          # job_id -> Job
JOBS_LOCK = threading.Lock()

# SSE event subscribers: job_id -> list[asyncio.Queue]
EVENT_SUBS = {}
EVENT_SUBS_LOCK = threading.Lock()
EVENT_LOOP = None   # set in startup() -- uvicorn's running loop


def _push_event(job_id, event):
    """Thread-safe: push an event dict to all SSE subscribers for this job.

    Uses the globally-stored EVENT_LOOP (captured at startup) rather than
    trying to resolve the loop dynamically, which fails in background threads
    on Python 3.10+.
    """
    global EVENT_LOOP
    loop = EVENT_LOOP
    if loop is None:
        return  # server not fully started yet, or loop wasn't captured
    with EVENT_SUBS_LOCK:
        queues = list(EVENT_SUBS.get(job_id, []))
    for q in queues:
        asyncio.run_coroutine_threadsafe(q.put(dict(event)), loop)

PORT = int(os.environ.get("MUSIC_DOWNLOADER_PORT", "8000"))


def _bind_host():
    """Interface uvicorn listens on. Precedence: env MUSIC_DOWNLOADER_BIND_HOST >
    config/settings.conf `bind_host` > default 0.0.0.0 (all interfaces).
    Set `bind_host` to your LAN IP to listen only on that NIC, which keeps
    the server off the Tailscale virtual interface.
    """
    env = os.environ.get("MUSIC_DOWNLOADER_BIND_HOST")
    if env:
        return env
    return read_conf(REPO / "config" / "settings.conf").get("bind_host") or "0.0.0.0"


BIND_HOST = _bind_host()


def _server_token():
    env = os.environ.get("MUSIC_DOWNLOADER_SERVER_TOKEN")
    if env:
        return env
    return read_conf(REPO / "config" / "settings.conf").get("server_token") or None


def _deezer_session_ok():
    """Best-effort liveness probe. dz.logged_in is only set at login and goes
    stale, so we make an auth-requiring call (get_user_data) and treat any
    exception as a dead/expired session (e.g. Deezer free-trial FLAC access
    lapsed -> it would silently hand back MP3s, which /health must catch)."""
    global DZ
    if DZ is None:
        return False
    try:
        DZ.gw.get_user_data()
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# job runner
# ---------------------------------------------------------------------------

def _run_job(job_id, url, user):
    """Background thread: run the playlist and stream progress into the job."""
    global DZ
    job = JOBS[job_id]
    lines = job.log
    tracks = job.progress.tracks
    work_dir = WORK_DIR_BASE / user
    work_dir.mkdir(parents=True, exist_ok=True)
    with DZ_LOCK:
        def on_progress(msg):
            lines.append(msg)
        def on_event(e):
            _push_event(job_id, e)
            t = e.get("type")
            if t == JobEventType.TRACKS:
                job.progress.total = e["total"]
                for item in e["items"]:
                    tracks[item["pos"]] = TrackState(
                        pos=item["pos"], name=item["name"],
                        status=DownloadStatus.PENDING, pct=0,
                    )
            elif t == JobEventType.START:
                if e["pos"] in tracks:
                    tracks[e["pos"]].status = DownloadStatus.DOWNLOADING
            elif t == JobEventType.PCT:
                for p in sorted(tracks.keys(), reverse=True):
                    if tracks[p].status == DownloadStatus.DOWNLOADING:
                        tracks[p].pct = e["pct"]
                        break
            elif t == JobEventType.DONE:
                pos = e["pos"]
                if pos in tracks:
                    tracks[pos].status = e["status"]
                    tracks[pos].pct = e["pct"]
        try:
            result = run_playlist(url, DZ, SETTINGS, work_dir=work_dir,
                                  on_progress=on_progress, on_event=on_event)
        except Exception as e:
            tb = traceback.format_exc()
            log.error("run_playlist raised: %s", e)
            for line in tb.strip().split("\n"):
                log.error("  %s", line)
            result = {"ok": False, "error": f"run_playlist raised: {e}", "traceback": tb}
    job.result = result
    job.status = DownloadStatus.OK if result.get("ok") else DownloadStatus.ERROR
    job.finished_at = time.strftime("%Y-%m-%dT%H:%M:%S")
    # Signal SSE subscribers that the job is finished.
    # Convert Path to str so the event is JSON-serializable.
    result_copy = dict(result)
    if "folder" in result_copy:
        result_copy["folder"] = str(result_copy["folder"])
    _push_event(job_id, {"type": JobEventType.JOB_DONE, "status": job.status,
                         "result": result_copy})


def _authenticate(request: Request):
    token = _server_token()
    if not token:
        return  # no token configured -> open (localhost use)
    provided = request.headers.get("X-Auth-Token")
    if provided != token:
        raise HTTPException(status_code=401, detail="missing/invalid X-Auth-Token")


# ---------------------------------------------------------------------------
# endpoints
# ---------------------------------------------------------------------------

# Belt-and-suspenders: clear the lock even on a hard Ctrl-C / process exit,
# not only on graceful shutdown (handled by the lifespan `finally`).
atexit.register(server_lock.clear_lock)


@app.get("/", response_class=HTMLResponse)
def index():
    html = (REPO / "templates" / "index.html")
    if html.is_file():
        return HTMLResponse(html.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>music-downloader</h1><p>templates/index.html not found.</p>")


@app.post("/download")
def download(request: Request, payload: dict):
    _authenticate(request)
    user = _require_user(request)
    url = (payload or {}).get("url", "")
    if not url:
        raise HTTPException(status_code=400, detail="missing 'url'")
    if DZ is None:
        raise HTTPException(status_code=503, detail="Deezer session not ready (ARL missing or login failed)")
    if not _deezer_session_ok():
        raise HTTPException(status_code=503, detail="Deezer session expired -- POST /reload with a fresh ARL")
    job_id = uuid.uuid4().hex[:12]
    with JOBS_LOCK:
        JOBS[job_id] = Job(
            id=job_id,
            url=url,
            user=user,
            started_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
        )
    threading.Thread(target=_run_job, args=(job_id, url, user), daemon=True).start()
    return {"job_id": job_id}


@app.get("/jobs")
def jobs(request: Request):
    user = _require_user(request)
    with JOBS_LOCK:
        items = sorted(JOBS.values(), key=lambda j: j.started_at, reverse=True)
    return {"jobs": [{
        "id": j.id, "url": j.url, "user": j.user,
        "status": j.status,
        "started_at": j.started_at, "finished_at": j.finished_at,
    } for j in items]}


@app.get("/jobs/{job_id}")
def job_status(job_id: str):
    with JOBS_LOCK:
        j = JOBS.get(job_id)
    if not j:
        raise HTTPException(status_code=404, detail="no such job")
    return {
        "id": j.id, "url": j.url, "status": j.status,
        "started_at": j.started_at, "finished_at": j.finished_at,
        "log": j.log[-200:], "result": j.result,
        "progress": asdict(j.progress),
    }


@app.get("/jobs/{job_id}/events")
async def job_events(job_id: str):
    """SSE stream: pushes download progress events to the browser in real time."""
    with JOBS_LOCK:
        if job_id not in JOBS:
            raise HTTPException(status_code=404, detail="no such job")

    queue = asyncio.Queue()
    with EVENT_SUBS_LOCK:
        EVENT_SUBS.setdefault(job_id, []).append(queue)

    async def event_stream():
        try:
            while True:
                evt = await queue.get()
                if evt is None:
                    return
                yield f"event: {evt['type'].value}\ndata: {json.dumps(evt)}\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            with EVENT_SUBS_LOCK:
                subs = EVENT_SUBS.get(job_id, [])
                if queue in subs:
                    subs.remove(queue)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.get("/users")
def get_users():
    users = read_users()
    return {"users": users, "default": users[0] if users else None}


@app.get("/playlists")
def playlists(request: Request):
    user = _require_user(request)
    return {"playlists": list_playlists(WORK_DIR_BASE / user)}


@app.get("/health")
def health():
    ok = _deezer_session_ok()
    return JSONResponse(
        status_code=200 if ok else 503,
        content={
            "deezer_session": "ok" if ok else "expired",
            "arl_present": ARL.is_file(),
            "jobs_running": sum(1 for j in JOBS.values() if j.status == "running"),
        },
    )


import zipfile, io

@app.get("/library/{folder}")
def library_folder(request: Request, folder: str):
    user = _require_user(request)
    fp = _resolve_user_path(user, folder)
    meta = fp / "playlist.meta.json"
    if not meta.is_file():
        raise HTTPException(status_code=404, detail="playlist.meta.json not found")
    data = json.loads(meta.read_text(encoding="utf-8"))
    tracks = data.get("tracks", []) or []
    # attach the on-disk filename (if present) using the same title-match the
    # downloader uses -- robust to the bare naming + Deezer suffixes.
    for t in tracks:
        flac = find_existing_track(fp, t.get("artist", "").split(), t.get("title", "")) \
            if t.get("status") == "downloaded" else None
        t["has_file"] = flac is not None
        t["filename"] = flac.name if flac else None
    return {
        "folder": folder,
        "name": data.get("name", folder),
        "spotify_url": data.get("spotify_url"),
        "tracks": tracks,
    }


@app.get("/library/{folder}/track/{position}")
def library_track_file(request: Request, folder: str, position: int):
    """Serve a single FLAC from a playlist folder by its Spotify position."""
    user = _require_user(request)
    fp = _resolve_user_path(user, folder)
    meta = fp / "playlist.meta.json"
    if not meta.is_file():
        raise HTTPException(status_code=404, detail="playlist.meta.json not found")
    data = json.loads(meta.read_text(encoding="utf-8"))
    tracks = data.get("tracks", []) or []
    t = next((x for x in tracks if x.get("position") == position), None)
    if not t:
        raise HTTPException(status_code=404, detail="no such track position")
    flac = find_existing_track(fp, t.get("artist", "").split(), t.get("title", ""))
    if not flac or not flac.is_file():
        raise HTTPException(status_code=404, detail="track file not found on disk")
    return FileResponse(
        str(flac),
        media_type="audio/flac",
        filename=flac.name,
    )


@app.get("/zip/{folder}")
def download_zip(request: Request, folder: str):
    user = _require_user(request)
    fp = _resolve_user_path(user, folder)
    if not fp.is_dir():
        raise HTTPException(status_code=404, detail="playlist folder not found")
    safe_name = "".join(c if c.isalnum() or c in " -_" else "_" for c in folder)
    filename = safe_name + ".zip"

    def gen():
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for fpath in sorted(fp.glob("*.flac")):
                zf.write(fpath, fpath.name)
        buf.seek(0)
        while True:
            chunk = buf.read(64 * 1024)
            if not chunk:
                break
            yield chunk

    return StreamingResponse(
        gen(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/reload")
def reload(request: Request, payload: dict = None):
    _authenticate(request)
    global DZ
    if not ARL.is_file():
        raise HTTPException(status_code=503, detail="deezer.arl missing")
    arl_text = ARL.read_text(encoding="utf-8").strip()
    sync_deezer_arl()
    try:
        DZ = init_deezer(arl_text)
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Deezer login failed: {e}")
    return {"ok": True, "deezer_session": "ok"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=BIND_HOST, port=PORT)
