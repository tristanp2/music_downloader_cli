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
(env MUSICDL_SERVER_TOKEN or config/settings.conf server_token). GETs are open
(localhost LAN use). Set the token or the endpoint stays unauthenticated.

Run:
  .venv/Scripts/python.exe -m uvicorn app:app --host 0.0.0.0 --port 8000
"""
import os
import sys
import time
import threading
import uuid
import atexit
import json
from pathlib import Path

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from core.config import resolve_output_dir, sync_deezer_arl, ARL, read_conf
from core.deezer import init_deezer
from core.downloader import run_playlist
from core.registry import list_playlists
from core import server_lock

REPO = Path(__file__).resolve().parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

app = FastAPI(title="music-downloader")

# ---------------------------------------------------------------------------
# server-side state (single process)
# ---------------------------------------------------------------------------
try:
    import deemix.settings as _dms
    SETTINGS = _dms.load(REPO / "config")
except Exception as _e:
    print(f"[warn] could not load deemix settings: {_e}")
    SETTINGS = {}

WORK_DIR = resolve_output_dir()
WORK_DIR.mkdir(parents=True, exist_ok=True)

DZ = None          # the one Deezer session, set in startup()
DZ_LOCK = threading.Lock()   # serialize Deezer calls (session not thread-safe)
JOBS = {}          # job_id -> job dict
JOBS_LOCK = threading.Lock()

PORT = int(os.environ.get("MUSICDL_PORT", "8000"))


def _server_token():
    env = os.environ.get("MUSICDL_SERVER_TOKEN")
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

def _run_job(job_id, url):
    """Background thread: run the playlist and stream progress into the job."""
    global DZ
    job = JOBS[job_id]
    job["progress"] = {"total": None, "tracks": {}}
    lines = job["log"]
    tracks = job["progress"]["tracks"]
    with DZ_LOCK:
        def on_progress(msg):
            lines.append(msg)
        def on_event(e):
            t = e.get("type")
            if t == "tracks":
                job["progress"]["total"] = e["total"]
                for item in e["items"]:
                    tracks[item["pos"]] = {
                        "pos": item["pos"], "name": item["name"],
                        "status": "pending", "pct": 0,
                    }
            elif t == "start":
                if e["pos"] in tracks:
                    tracks[e["pos"]]["status"] = "downloading"
            elif t == "pct":
                for p in sorted(tracks.keys(), reverse=True):
                    if tracks[p]["status"] == "downloading":
                        tracks[p]["pct"] = e["pct"]
                        break
            elif t == "done":
                pos = e["pos"]
                if pos in tracks:
                    tracks[pos].update(status=e["status"], pct=e["pct"])
        try:
            result = run_playlist(url, DZ, SETTINGS, work_dir=WORK_DIR,
                                  on_progress=on_progress, on_event=on_event)
        except Exception as e:
            result = {"ok": False, "error": f"run_playlist raised: {e}"}
    job["result"] = result
    job["status"] = "done" if result.get("ok") else "error"
    job["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")


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

@app.on_event("startup")
def _startup():
    global DZ
    if not ARL.is_file():
        print("[startup] deezer.arl missing -- /download will 503 until added")
        return
    arl_text = ARL.read_text(encoding="utf-8").strip()
    sync_deezer_arl()
    try:
        DZ = init_deezer(arl_text)
        print("[startup] Deezer session established")
    except Exception as e:
        print(f"[startup] Deezer login failed: {e}")
    server_lock.write_lock(PORT)


@app.on_event("shutdown")
def _shutdown():
    server_lock.clear_lock()


# Belt-and-suspenders: clear the lock even on a hard Ctrl-C / process exit,
# not only on the graceful FastAPI shutdown event.
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
    url = (payload or {}).get("url", "")
    if not url:
        raise HTTPException(status_code=400, detail="missing 'url'")
    if DZ is None:
        raise HTTPException(status_code=503, detail="Deezer session not ready (ARL missing or login failed)")
    if not _deezer_session_ok():
        raise HTTPException(status_code=503, detail="Deezer session expired -- POST /reload with a fresh ARL")
    job_id = uuid.uuid4().hex[:12]
    with JOBS_LOCK:
        JOBS[job_id] = {
            "id": job_id,
            "url": url,
            "status": "running",
            "log": [],
            "result": None,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "finished_at": None,
        }
    threading.Thread(target=_run_job, args=(job_id, url), daemon=True).start()
    return {"job_id": job_id}


@app.get("/jobs")
def jobs():
    with JOBS_LOCK:
        items = sorted(JOBS.values(), key=lambda j: j["started_at"], reverse=True)
    return {"jobs": [{
        "id": j["id"], "url": j["url"], "status": j["status"],
        "started_at": j["started_at"], "finished_at": j["finished_at"],
        "log": j["log"][-200:], "result": j["result"],
        "progress": j.get("progress"),
    } for j in items]}


@app.get("/jobs/{job_id}")
def job_status(job_id: str):
    with JOBS_LOCK:
        j = JOBS.get(job_id)
    if not j:
        raise HTTPException(status_code=404, detail="no such job")
    return {
        "id": j["id"], "url": j["url"], "status": j["status"],
        "started_at": j["started_at"], "finished_at": j["finished_at"],
        "log": j["log"][-200:], "result": j["result"],
        "progress": j.get("progress"),
    }


@app.get("/playlists")
def playlists():
    return {"playlists": list_playlists(WORK_DIR)}


@app.get("/health")
def health():
    ok = _deezer_session_ok()
    return JSONResponse(
        status_code=200 if ok else 503,
        content={
            "deezer_session": "ok" if ok else "expired",
            "arl_present": ARL.is_file(),
            "jobs_running": sum(1 for j in JOBS.values() if j["status"] == "running"),
        },
    )


import zipfile, io

@app.get("/library/{folder}")
def library_folder(folder: str):
    # resolve safely under WORK_DIR
    fp = (WORK_DIR / folder).resolve()
    if not str(fp).startswith(str(WORK_DIR.resolve())):
        raise HTTPException(status_code=403, detail="invalid path")
    meta = fp / "playlist.meta.json"
    if not meta.is_file():
        raise HTTPException(status_code=404, detail="playlist.meta.json not found")
    data = json.loads(meta.read_text(encoding="utf-8"))
    tracks = data.get("tracks", []) or []
    # attach filename if it exists on disk
    for t in tracks:
        nn = f"{t['position']:02d}"
        t["has_file"] = any(
            f.name.startswith(nn) and f.name.endswith(".flac")
            for f in fp.glob("*.flac")
        ) if t.get("status") == "downloaded" else False
    return {
        "folder": folder,
        "name": data.get("name", folder),
        "spotify_url": data.get("spotify_url"),
        "tracks": tracks,
    }


@app.get("/zip/{folder}")
def download_zip(folder: str):
    # resolve safely under WORK_DIR only
    fp = (WORK_DIR / folder).resolve()
    if not str(fp).startswith(str(WORK_DIR.resolve())):
        raise HTTPException(status_code=403, detail="invalid path")
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
    uvicorn.run(app, host="0.0.0.0", port=PORT)
