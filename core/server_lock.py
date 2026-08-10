"""Server lockfile: the single signal that the web server owns the Deezer
session, and the discovery mechanism for the CLI / sync clients.

Design
------
- The web server, while running, writes REPO/.server.lock containing its PID
  and listen port, and removes it on shutdown.
- A second `app.py` sees the lock and refuses to start (one session only).
- The CLI checks the lock first:
    * present AND the PID is still alive  -> POST the URL to the server.
    * absent (or stale PID)               -> run locally via the core lib.
  Because only one process holds the lock at a time, the CLI's local fallback
  only runs when the server is DOWN -- so the two never hold a Deezer session
  simultaneously (which would invalidate one of them on a shared ARL).

The PID check is a real liveness probe (os.kill(pid, 0)), not just "does the
file exist", so a crashed server leaves a stale lock that the CLI correctly
ignores and falls back to local mode.
"""
import json
import os
import sys
from pathlib import Path

from .config import REPO, read_conf

LOCK_PATH = REPO / ".server.lock"


def _pid_alive(pid):
    """Return True if a process with this PID currently exists (cross-platform)."""
    if os.name == "nt":
        # On Windows, OpenProcess + a no-op signal check. os.kill(pid, 0) works.
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def write_lock(port):
    """Create / refresh the lockfile with our PID + port. Overwrites any
    stale lock left by a previous run."""
    payload = {"pid": os.getpid(), "port": port}
    LOCK_PATH.write_text(json.dumps(payload), encoding="utf-8")


def clear_lock():
    """Remove the lockfile if we own it (matches our PID)."""
    try:
        if LOCK_PATH.is_file():
            data = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
            if data.get("pid") == os.getpid():
                LOCK_PATH.unlink()
    except Exception:
        pass


def read_lock():
    """Return (pid, port) from a live lock, or None if no live server.

    A lock is 'live' only if the recorded PID is still running. A stale lock
    (server crashed) returns None so callers fall back to local mode.
    """
    if not LOCK_PATH.is_file():
        return None
    try:
        data = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
        pid = data.get("pid")
        port = data.get("port")
        if pid is None or port is None:
            return None
        if not _pid_alive(pid):
            return None
        return (pid, port)
    except Exception:
        return None


def server_is_up():
    """Convenience: return the live server port, or None."""
    live = read_lock()
    return live[1] if live else None


def another_server_running():
    """True if a DIFFERENT live process holds the lock (refuse to start)."""
    live = read_lock()
    if not live:
        return False
    pid, _ = live
    return pid != os.getpid()


def _server_token():
    """Shared secret for POST /download. Source: env MUSICDL_SERVER_TOKEN,
    then config/settings.conf `server_token`. None if unset."""
    env = os.environ.get("MUSICDL_SERVER_TOKEN")
    if env:
        return env
    cfg = read_conf(REPO / "config" / "settings.conf")
    return cfg.get("server_token") or None


def cli_dispatch(url, dz=None, settings=None, work_dir=None):
    """Route a download request: POST to the running server if up, else run
    locally via the core lib.

    When running locally, `dz`/`settings`/`work_dir` must be supplied (the CLI
    builds its own session). Returns the result dict from run_playlist either
    way. Raises nothing for the local path's auth/parse errors -- those are
    already handled inside run_playlist, which returns an error dict.
    """
    port = server_is_up()
    if port is not None:
        return _post_to_server(port, url)
    # local fallback
    if dz is None or settings is None:
        raise RuntimeError("cli_dispatch local mode requires dz + settings")
    from .downloader import run_playlist
    return run_playlist(url, dz, settings, work_dir=work_dir)


def _post_to_server(port, url):
    """POST {url} to the running server's /download endpoint. Returns the
    server's JSON result dict. Network errors surface as an error dict so the
    CLI can still report cleanly."""
    import urllib.request
    import urllib.error

    token = _server_token()
    body = json.dumps({"url": url}).encode("utf-8")
    req = urllib.request.Request(
        f"http://localhost:{port}/download",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    if token:
        req.add_header("X-Auth-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        return {"ok": False, "error": f"server returned HTTP {e.code}: {detail}"}
    except Exception as e:
        return {"ok": False, "error": f"could not reach server on port {port}: {e}"}
