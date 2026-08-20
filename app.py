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
from __future__ import annotations

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
from collections.abc import AsyncGenerator, Iterator
from dataclasses import asdict
from dataclasses import is_dataclass

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles

from core.config import resolve_output_dir, ARL, read_conf, read_users, CONF_SETTINGS
from core.deezer import init_deezer
from deezer import Deezer
from core.downloader import run_playlist, DispatchResult
from core.spotify import validate_spotify_url, get_spotify_token, parse_spotify_playlist
from core.registry import list_playlists
from core.library import find_existing_track
from core import server_lock
from core import log
from core import attach_uvicorn_loggers
from core.job import DownloadJob, JobProgress, TrackState
from core.event_types import JobEventType, DownloadStatus


PORT = int(os.environ.get("MUSIC_DOWNLOADER_PORT", "8000")) 

REPO = Path(__file__).resolve().parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


from contextlib import asynccontextmanager


@asynccontextmanager
async def lifespan(app):
    global DZ, EVENT_LOOP
    EVENT_LOOP = asyncio.get_running_loop()
    attach_uvicorn_loggers()
    if server_lock.another_server_running():
        log.error("[startup] another music-downloader server is already running "
                  "(REPO/.server.lock points at a live PID) -- refusing to start")
        return
    if not ARL.is_file():
        log.info("[startup] config/.arl missing -- /download will 503 until added")
    else:
        arl_text = ARL.read_text(encoding="utf-8").strip()
        try:
            DZ = init_deezer(arl_text)
            log.info("[startup] Deezer session established")
        except Exception as e:
            log.warning("[startup] Deezer login failed: %s", e)
    server_lock.write_lock(PORT)
    # Periodic sync: re-enqueue every known playlist as a job on an interval
    # (default 12h). 0 or negative in settings.conf disables it.
    _sync_interval = float((read_conf(CONF_SETTINGS).get("sync_interval_hours") or "12") or 12)
    start_scheduler(_sync_interval)
    start_liveness_log(60)
    try:
        yield
    finally:
        # Close any open SSE connections (job feed + per-job event streams) so
        # that Ctrl-C / shutdown ends them cleanly instead of leaving the
        # generators blocked on queue.get() until the OS kills the socket.
        # Each event_stream generator returns on a None sentinel.
        with JOB_FEED_LOCK:
            for q in JOB_FEED_SUBS:
                try:
                    q.put_nowait(None)
                except Exception:
                    pass
        with EVENT_SUBS_LOCK:
            for subs in EVENT_SUBS.values():
                for q in subs:
                    try:
                        q.put_nowait(None)
                    except Exception:
                        pass
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

DZ: Deezer | None = None          # the one Deezer session, set in startup()
JOBS: dict[str, DownloadJob] = {}          # job_id -> Job
JOBS_LOCK = threading.Lock()

# SSE event subscribers: job_id -> list[asyncio.Queue]
EVENT_SUBS: dict[str, list[asyncio.Queue]] = {}
EVENT_SUBS_LOCK = threading.Lock()
EVENT_LOOP: asyncio.AbstractEventLoop | None = None   # set in startup() -- uvicorn's running loop


def _push_event(job_id: str, event: dict) -> None:
    """Thread-safe: push an event dict to all SSE subscribers for this job.

    Uses the globally-stored EVENT_LOOP (captured at startup) rather than
    trying to resolve the loop dynamically, which fails in background threads
    on Python 3.10+.
    """
    global EVENT_LOOP
    loop = EVENT_LOOP
    if loop is None or loop.is_closed():
        return  # server not fully started, or loop already torn down
    with EVENT_SUBS_LOCK:
        queues = list(EVENT_SUBS.get(job_id, []))
    for q in queues:
        try:
            asyncio.run_coroutine_threadsafe(q.put(dict(event)), loop)
        except RuntimeError:
            # Event loop is closing/closed (e.g. during Ctrl-C shutdown) ->
            # no subscribers are listening, so just drop the event rather
            # than let the worker thread throw "Event loop is closed" and
            # dump a giant traceback at process exit.
            return
    # Relay track-level progress (tracks/start/pct/done) to the shared feed too,
    # so every connected tab sees live per-track progress for ANY job -- not just
    # the creator's per-job SSE. job_done is already covered by JOB_DONE broadcast.
    t = event.get("type")
    if t in (JobEventType.TRACKS, JobEventType.START, JobEventType.PCT, JobEventType.DONE):
        _push_job_feed({"type": t, "job_id": job_id, **{k: v for k, v in event.items() if k != "type"}})


# Shared job-feed subscribers: ONE SSE per browser tab (opened at page load),
# not per-job. Carries job_created / job_done for EVERY job regardless of which
# user created it, so all simultaneously-connected browsers see new jobs and
# completions in real time (no 10s poll dependency for cross-user visibility).
JOB_FEED_SUBS: list[asyncio.Queue] = []
JOB_FEED_LOCK = threading.Lock()


def _job_public(job: DownloadJob) -> dict:
    """JSON-friendly snapshot of a DownloadJob for the shared feed."""
    return {
        "id": job.id, "url": job.url, "user": job.user, "playlist_name": job.playlist_name,
        "status": job.status,
        "started_at": job.started_at, "finished_at": job.finished_at,
        # Include progress so a finished job's track rows survive the 10s poll
        # (GET /jobs now also returns it) and stay visible until the card cap
        # pushes them out (i.e. only when another job starts and this one scrolls out).
        "progress": asdict(job.progress),
        # Result carries the downloaded/skipped/missed/failed breakdown so the
        # frontend can render stats chips in the job header without expanding.
        "result": _jsonable(job.result),
    }


def _jsonable(v: object) -> object:
    """Recursively coerce non-JSON types (notably pathlib.Path / WindowsPath
    and dataclasses like DispatchResult) to JSON-friendly shapes. Guarantees
    nothing in a serialized payload can raise 'Object of type ... is not JSON
    serializable' en route to the frontend."""
    if isinstance(v, Path):
        return str(v)
    if isinstance(v, dict):
        return {k: _jsonable(val) for k, val in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if is_dataclass(v) and not isinstance(v, type):
        return _jsonable(asdict(v))
    return v


def _dumps(obj: object) -> str:
    """json.dumps with a Path-safe fallback. Used by every SSE generator so that
    ANY event carrying a filesystem path (e.g. a download result's folder) is
    serialized as a string instead of crashing the stream mid-job."""
    return json.dumps(obj, default=_jsonable)


def _push_job_feed(event: dict) -> None:
    """Broadcast a job-feed event to every connected tab. Same loop-safe
    pattern as _push_event (uses the global EVENT_LOOP captured at startup)."""
    global EVENT_LOOP
    loop = EVENT_LOOP
    if loop is None or loop.is_closed():
        return  # server not fully started, or loop already torn down
    with JOB_FEED_LOCK:
        queues = list(JOB_FEED_SUBS)
    for q in queues:
        try:
            asyncio.run_coroutine_threadsafe(q.put(dict(event)), loop)
        except RuntimeError:
            # Same as _push_event: drop on a closed loop instead of throwing
            # "Event loop is closed" from the worker thread at shutdown.
            return




def _bind_host() -> str:
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


def _server_token() -> str | None:
    env = os.environ.get("MUSIC_DOWNLOADER_SERVER_TOKEN")
    if env:
        return env
    return read_conf(REPO / "config" / "settings.conf").get("server_token") or None


def _deezer_session_ok() -> bool:
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
# job runner -- SEQUENTIAL drain
# ---------------------------------------------------------------------------
# One worker thread owns the Deezer session exclusively and runs jobs FIFO from
# JOB_QUEUE, so only one download is ever in flight at a time (the session is
# not thread-safe, so serializing the whole job -- not just the fetch -- is the
# simplest correct design and also makes the job status honest: queued -> running
# -> done/error). DZ_LOCK is gone; nothing else touches DZ.

from collections import deque
JOB_QUEUE: deque[str] = deque()
JOB_QUEUE_LOCK = threading.Lock()
_drain_thread: threading.Thread | None = None


def _ensure_drain_thread() -> None:
    """Start the single sequential worker thread if it isn't already running."""
    global _drain_thread
    with JOB_QUEUE_LOCK:
        if _drain_thread is None or not _drain_thread.is_alive():
            _drain_thread = threading.Thread(target=_process_job_queue, daemon=True)
            _drain_thread.start()


def _process_job_queue() -> None:
    """Pop job_ids FIFO and run each to completion before starting the next."""
    global DZ
    while True:
        with JOB_QUEUE_LOCK:
            if not JOB_QUEUE:
                _drain_thread = None
                return
            job_id = JOB_QUEUE.popleft()
        url, user = JOBS[job_id].url, JOBS[job_id].user
        _run_job(job_id, url, user)


def _run_job(job_id: str, url: str, user: str) -> None:
    """Run one playlist to completion, streaming progress into the job.

    Called only from the drain thread, which holds exclusive use of the Deezer
    session -- so no lock is needed around run_playlist.
    """
    global DZ
    job = JOBS[job_id]
    lines = job.log
    tracks = job.progress.tracks
    work_dir = WORK_DIR_BASE / user
    work_dir.mkdir(parents=True, exist_ok=True)

    def on_progress(msg: str) -> None:
        lines.append(msg)

    def on_event(e: dict) -> None:
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

    dz_local = DZ
    if dz_local is None:
        result = DispatchResult(ok=False, error="Deezer session not ready")
    else:
        # The download actually begins here. Stamp the true start time and
        # broadcast it so the frontend shows this, not the enqueue/sync time.
        # Status only flips to "running" NOW -- not at enqueue -- so queued jobs
        # honestly sit at "queued" until the one ahead of them finishes.
        with JOBS_LOCK:
            job.status = "running"
            job.started_at = time.strftime("%Y-%m-%dT%H:%M:%S")
        _push_event(job_id, {"type": JobEventType.JOB_STARTED, "started_at": job.started_at})
        _push_job_feed({"type": JobEventType.JOB_STARTED, "job": _job_public(job)})
        try:
            result = run_playlist(url, dz_local, SETTINGS, work_dir=work_dir,
                                  on_progress=on_progress, on_event=on_event)
        except Exception as e:
            tb = traceback.format_exc()
            log.error("run_playlist raised: %s", e)
            for line in tb.strip().split("\n"):
                log.error("  %s", line)
            result = DispatchResult(ok=False, error=f"run_playlist raised: {e}")
    job.result = result
    job.status = DownloadStatus.OK if result.ok else DownloadStatus.ERROR
    job.playlist_name = str(result.name or "")
    job.finished_at = time.strftime("%Y-%m-%dT%H:%M:%S")
    # Signal SSE subscribers that the job is finished.
    _push_event(job_id, {"type": JobEventType.JOB_DONE, "status": job.status,
                         "result": _jsonable(result)})
    # Also broadcast completion to the shared feed so every tab sees it live.
    _push_job_feed({"type": JobEventType.JOB_DONE, "job": _job_public(job)})


def _authenticate(request: Request) -> None:
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
def index() -> HTMLResponse:
    html = (REPO / "templates" / "index.html")
    if html.is_file():
        return HTMLResponse(html.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>music-downloader</h1><p>templates/index.html not found.</p>")


# Serve the split-out frontend JS (templates/index.html references /static/app.js).
app.mount("/static", StaticFiles(directory=str(REPO / "static")), name="static")


def _start_job(url: str, user: str, name: str = "") -> str | None:
    """Create a Job and enqueue it for the sequential drain worker. Shared by
    POST /download and the sync enqueuer so every start goes through one path.
    `name` is the resolved playlist title, populated up-front by the manual
    /download path so the card shows it immediately; sync passes "" and the
    worker fills it on completion.

    Jobs run ONE AT A TIME (FIFO) on the single drain thread -- see _drain_jobs.
    A new job sits at status "queued" until every job ahead of it finishes, so
    the card honestly reflects wait-vs-running.

    The "already active?" check (against both live jobs and the pending queue)
    and the Job insert happen atomically under JOBS_LOCK so two concurrent
    requests can't double-enqueue the same (user, url). Returns the new
    job_id, or None if an active/queued job for this (user, url) already exists.
    Callers turn the None into a 409 / skip."""
    with JOBS_LOCK:
        for job in JOBS.values():
            if (job.user == user and job.url == url
                    and job.status not in (DownloadStatus.OK, DownloadStatus.ERROR)):
                return None
        for job_id in JOB_QUEUE:
            j = JOBS.get(job_id)
            if j is not None and j.user == user and j.url == url:
                return None
        job_id = uuid.uuid4().hex[:12]
        JOBS[job_id] = DownloadJob(
            id=job_id,
            url=url,
            user=user,
            playlist_name=name,
        )
    # Broadcast to the shared job-feed so every connected tab sees the new job
    # immediately (not just the creator's per-job SSE, and not waiting on a poll).
    _push_job_feed({"type": JobEventType.JOB_CREATED, "job": _job_public(JOBS[job_id])})
    # Enqueue and ensure the drain thread is alive. The job stays "queued" until
    # it reaches the head of JOB_QUEUE.
    with JOB_QUEUE_LOCK:
        JOB_QUEUE.append(job_id)
    _ensure_drain_thread()
    return job_id


# ---------------------------------------------------------------------------
# sync: re-enqueue every known playlist as a normal job (filesystem-as-registry)
# ---------------------------------------------------------------------------

# Guards against overlapping syncs (periodic timer or manual trigger firing
# while a previous sync is still walking/enqueuing). Enqueue is fast, but the
# guard also keeps a manual /sync from stacking on top of the scheduled one.
_SYNC_LOCK = threading.Lock()


def _derive_user_for_folder(folder_path: Path) -> str:
    """Given a playlist folder under WORK_DIR_BASE, return its owning user.

    Layout is WORK_DIR_BASE/<user>/<folder>. If the folder sits directly under
    the base (no user layer), fall back to the first configured user so the
    job still has a valid whitelist entry.
    """
    try:
        rel = folder_path.resolve().relative_to(WORK_DIR_BASE.resolve())
        parts = rel.parts
        if len(parts) >= 2:
            return parts[0]
    except Exception:
        pass
    users = read_users()
    return users[0] if users else "tristan"


def enqueue_sync_all() -> dict[str, object]:
    """Walk the filesystem registry and enqueue a normal job for each known
    playlist's Spotify URL. Returns {"queued": N, "jobs": [job_id...], "skipped": reason}.

    Deliberately enqueues EVERY playlist -- including ones that are 100%
    downloaded -- because the point of a sync is to re-check the Spotify
    playlist for NEW tracks. The "only fetch what's missing" behaviour lives in
    core.library.find_existing_track (run_playlist skips already-present FLACs),
    so re-running a complete playlist is cheap and only pulls genuinely new
    tracks. Do NOT filter on downloaded/missed counts here -- that would stop
    sync from noticing added Spotify tracks.

    STRICTLY ADDITIVE: sync (and run_playlist) never delete a downloaded FLAC
    just because a track was removed from the Spotify playlist. The only
    deletion that ever happens is cleanup of a PARTIAL/interrupted download
    (core.library.find_partial_track -> incomplete FLAC). Do NOT add "prune
    removed tracks" logic -- the user wants removals from Spotify to be ignored,
    not reflected on disk.

    Skips (and reports) when the Deezer session isn't ready -- a sync with no
    session would just 503 every job.
    """
    if DZ is None or not _deezer_session_ok():
        return {"queued": 0, "jobs": [], "skipped": "deezer session not ready"}
    playlists = list_playlists(WORK_DIR_BASE)
    jobs = []
    for pl in playlists:
        url = pl.get("spotify_url")
        folder = pl.get("folder")
        if not url or not folder:
            continue
        # Prefer the user the playlist was originally downloaded under
        # (encoded in its filesystem location WORK_DIR_BASE/<user>/<folder>).
        # Fall back to deriving from the folder path in case the "user" field
        # is missing (e.g. older/foreign meta files).
        user = pl.get("user") or _derive_user_for_folder(WORK_DIR_BASE / folder)
        name = pl.get("name") or ""
        # _start_job performs the "already active?" check atomically and returns
        # None if a job for this (user, url) is already queued/running. This
        # stops a manual /sync (or a fast double-click, or two users syncing at
        # once) from queuing every playlist twice while the first batch runs.
        jid = _start_job(url, user, name=name)
        if jid is None:
            continue
        jobs.append(jid)
    return {"queued": len(jobs), "jobs": jobs, "skipped": None}


def start_scheduler(interval_hours: float) -> None:
    """Background thread: every interval_hours, enqueue a sync of all playlists.

    Runs only while the interpreter is live (daemon thread, no catch-up for
    missed intervals -- a box asleep for a day just syncs once on wake). The
    _SYNC_LOCK prevents a manual /sync from overlapping the scheduled one. If a
    sync is already in flight, the scheduled tick is skipped (next tick retries).
    """
    if not interval_hours or interval_hours <= 0:
        log.info("[scheduler] disabled (interval <= 0)")
        return
    interval = interval_hours * 3600.0

    def _loop() -> None:
        while True:
            time.sleep(interval)
            if not _SYNC_LOCK.acquire(blocking=False):
                log.info("[scheduler] sync already running -- skipping this tick")
                continue
            try:
                log.info("[scheduler] periodic sync starting")
                res = enqueue_sync_all()
                log.info("[scheduler] periodic sync enqueued %s jobs (%s)",
                         res.get("queued"), res.get("skipped") or "ok")
            except Exception as e:
                log.error("[scheduler] sync failed: %s", e)
            finally:
                _SYNC_LOCK.release()

    t = threading.Thread(target=_loop, name="sync-scheduler", daemon=True)
    t.start()
    log.info("[scheduler] started, interval = %s h", interval_hours)


def start_liveness_log(interval_seconds: int = 60) -> None:
    """Background heartbeat: every interval, emit a one-line [alive] log so the
    file carries evidence the server is up + how many clients are connected.

    Reuses the shared job-feed SSE subscriber list (one subscriber == one open
    browser tab) for the connected-client count, and JOBS for active jobs.
    No new machinery -- same daemon-thread + time.sleep pattern as start_scheduler.
    """
    def _loop() -> None:
        while True:
            time.sleep(interval_seconds)
            try:
                with JOB_FEED_LOCK:
                    clients = len(JOB_FEED_SUBS)
                with JOBS_LOCK:
                    active = sum(1 for j in JOBS.values() if j.status == "running")
                log.info("[alive] server up -- %d connected client(s), %d active job(s)",
                         clients, active)
            except Exception as e:
                log.error("[alive] liveness log failed: %s", e)
    t = threading.Thread(target=_loop, name="liveness-log", daemon=True)
    t.start()
    log.info("[alive] liveness log started, interval = %s s", interval_seconds)


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

    # Resolve + validate the playlist identity BEFORE it enters the queue. A
    # garbage URL must surface at the input box, never as a nameless job card.
    playlist_id = validate_spotify_url(url)
    if not playlist_id:
        raise HTTPException(status_code=400, detail="Invalid Spotify playlist URL")
    try:
        token = get_spotify_token()
        parsed = parse_spotify_playlist(token, playlist_id)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not resolve Spotify playlist: {e}")
    name = (parsed.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Spotify playlist has no name")

    # _start_job checks "already active?" atomically (under JOBS_LOCK) and
    # returns None if a job for this (user, url) is already queued/running --
    # so a duplicate can't slip through two concurrent requests (e.g. two users
    # hitting send during the same Spotify lookup). Tristan and marc can each
    # queue the same playlist; only a second request for the SAME user is 409'd.
    job_id = _start_job(url, user, name=name)
    if job_id is None:
        raise HTTPException(
            status_code=409,
            detail="A download for this playlist is already in progress",
        )
    return {"job_id": job_id}


@app.post("/sync")
def sync(request: Request) -> dict:
    """Manually trigger a sync: enqueue a normal job for every known playlist.

    Auth-gated like /download. If a sync (periodic or manual) is already in
    flight, returns 409 so the caller knows to wait rather than stack another.
    The enqueued jobs appear in the normal queue + SSE -- no separate UI.
    """
    _authenticate(request)
    if not _SYNC_LOCK.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="sync already running")
    try:
        res = enqueue_sync_all()
    finally:
        _SYNC_LOCK.release()
    if res.get("skipped"):
        raise HTTPException(status_code=503, detail=res["skipped"])
    return {"queued": res["queued"], "jobs": res["jobs"]}


@app.get("/jobs")
def jobs(request: Request) -> dict:
    user = _require_user(request)
    with JOBS_LOCK:
        items = sorted(JOBS.values(), key=lambda j: j.started_at, reverse=True)
    return {"jobs": [_job_public(j) for j in items]}


@app.get("/jobs/stream")
async def job_feed_stream(request: Request):
    """Shared SSE: ONE connection per browser tab (opened at page load).

    Registered BEFORE /jobs/{job_id} so the static path wins over the
    parameterized one (Starlette matches by registration order, not by
    specificity). Broadcasts job_created / job_done for EVERY job regardless
    of which user created it, so all simultaneously-connected tabs see new
    jobs and completions in real time. On connect it also replays the current
    job list as job_created events, so a tab opened mid-run immediately shows
    what's already in flight. Auth: open (same as /jobs); the feed carries no
    secrets.
    """
    queue = asyncio.Queue()
    with JOB_FEED_LOCK:
        JOB_FEED_SUBS.append(queue)

    # Replay existing jobs so a freshly-opened tab is in sync at once.
    with JOBS_LOCK:
        existing = [j for j in JOBS.values()]
    for j in existing:
        await queue.put({"type": JobEventType.JOB_CREATED, "job": _job_public(j)})

    async def event_stream() -> AsyncGenerator[str, None]:
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    evt = await asyncio.wait_for(queue.get(), timeout=2.0)
                except asyncio.TimeoutError:
                    # Wake periodically so a client disconnect (e.g. Ctrl-C)
                    # is noticed without waiting for the next pushed event.
                    # uvicorn closes the connection BEFORE running the lifespan
                    # shutdown, so we must detect the disconnect ourselves to
                    # exit cleanly instead of hanging on queue.get().
                    continue
                if evt is None:
                    return
                yield f"event: {evt['type'].value}\ndata: {_dumps(evt)}\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            with JOB_FEED_LOCK:
                if queue in JOB_FEED_SUBS:
                    JOB_FEED_SUBS.remove(queue)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.get("/jobs/{job_id}")
def job_status(job_id: str):
    with JOBS_LOCK:
        j = JOBS.get(job_id)
    if not j:
        raise HTTPException(status_code=404, detail="no such job")
    return {
        "id": j.id, "url": j.url, "status": j.status,
        "started_at": j.started_at, "finished_at": j.finished_at,
        "log": j.log[-200:], "result": _jsonable(j.result),
        "progress": asdict(j.progress),
    }


@app.get("/users")
def get_users() -> dict:
    users = read_users()
    return {"users": users, "default": users[0] if users else None}


@app.get("/playlists")
def playlists(request: Request) -> dict:
    user = _require_user(request)
    return {"playlists": list_playlists(WORK_DIR_BASE / user)}


@app.get("/health")
def health() -> JSONResponse:
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
    # Build a on-disk presence index ONCE (one glob + one tag-parse pass over
    # the folder) instead of calling find_existing_track() per track -- the old
    # path re-scanned + re-parsed every audio file for every track, which is an
    # O(tracks * files) tag-read storm that made large playlists crawl.
    present_index = core.library._index_present_tracks(fp)
    for t in tracks:
        flac = find_existing_in_index(present_index, t.get("title", "")) \
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

    def gen() -> Iterator[bytes]:
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
def reload(request: Request, payload: dict | None = None):
    _authenticate(request)
    global DZ
    if not ARL.is_file():
        raise HTTPException(status_code=503, detail="config/.arl missing")
    arl_text = ARL.read_text(encoding="utf-8").strip()
    try:
        DZ = init_deezer(arl_text)
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Deezer login failed: {e}")
    return {"ok": True, "deezer_session": "ok"}


if __name__ == "__main__":
    import uvicorn
    # Lower the graceful-shutdown timeout. On Ctrl-C uvicorn waits for in-flight
    # SSE connections to finish BEFORE running the lifespan shutdown -- and an
    # open SSE stream never "finishes" on its own. With the default 30s timeout
    # the server hangs waiting for those connections (the "Waiting for
    # connections to close" message). The SSE generators self-detect a dropped
    # client (see the wake-on-timeout loop in event_stream), so a short grace
    # window is enough: one Ctrl-C ends the server in ~2s, no second Ctrl-C.
    uvicorn.run(app, host=BIND_HOST, port=PORT, timeout_graceful_shutdown=3)
