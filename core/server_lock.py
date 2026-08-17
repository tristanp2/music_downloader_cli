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

The PID check uses psutil.pid_exists() (a real, cross-platform liveness probe),
not just "does the file exist", so a crashed server leaves a stale lock that the
CLI correctly ignores and falls back to local mode.
"""
from __future__ import annotations

import json
import os
import psutil
import sys
from urllib.parse import urlencode
from pathlib import Path

from .config import REPO, read_conf
from deezer import Deezer

LOCK_PATH = REPO / ".server.lock"


def _bind_host() -> str:
    """Host the server listens on (config bind_host), matching app.py's bind.

    If the server binds all interfaces (0.0.0.0) we use loopback; if it binds a
    specific LAN IP we use that IP -- never localhost, which wouldn't connect to
    a non-loopback bind.
    """
    host = (read_conf(REPO / "config" / "settings.conf").get("bind_host") or "0.0.0.0").strip()
    return "127.0.0.1" if host in ("0.0.0.0", "") else host

def write_lock(port: int) -> None:
    """Create / refresh the lockfile with our PID + port.

    If an existing lock points at a DEAD PID (server crashed / hard-killed),
    reap it first so we never refuse to start behind a stale lock. A lock with
    a LIVE foreign PID means a real second server is up -- leave it (the caller
    checks another_server_running() and bails).
    """
    try:
        if LOCK_PATH.is_file():
            try:
                data = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
                stale_pid = data.get("pid")
                if stale_pid is not None and not psutil.pid_exists(stale_pid):
                    LOCK_PATH.unlink()
            except Exception:
                # unreadable lock -> treat as stale and replace
                LOCK_PATH.unlink()
    except Exception:
        pass
    payload = {"pid": os.getpid(), "port": port}
    LOCK_PATH.write_text(json.dumps(payload), encoding="utf-8")


def clear_lock() -> None:
    """Remove the lockfile. PID-agnostic: on Windows SIGTERM is a hard kill that
    never runs shutdown/atexit, so gating on our own PID would leave the lock
    orphaned. Whoever is shutting down owns the lock at that moment, so just
    delete it if present. The PID-liveness check in read_lock()/another_server_running()
    is what makes a stale lock from a crash harmless (it's ignored and reaped on
    next start).
    """
    try:
        if LOCK_PATH.is_file():
            LOCK_PATH.unlink()
    except Exception:
        pass


def read_lock() -> tuple[int, int] | None:
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
        if not psutil.pid_exists(pid):
            return None
        return (pid, port)
    except Exception:
        return None


def server_is_up() -> int | None:
    """Convenience: return the live server port, or None."""
    live = read_lock()
    return live[1] if live else None


def another_server_running() -> bool:
    """True if a DIFFERENT live process holds the lock (refuse to start)."""
    live = read_lock()
    if not live:
        return False
    pid, _ = live
    return pid != os.getpid()


def _server_token() -> str | None:
    """Shared secret for POST /download. Source: env MUSIC_DOWNLOADER_SERVER_TOKEN,
    then config/settings.conf `server_token`. None if unset."""
    env = os.environ.get("MUSIC_DOWNLOADER_SERVER_TOKEN")
    if env:
        return env
    cfg = read_conf(REPO / "config" / "settings.conf")
    return cfg.get("server_token") or None


def cli_dispatch(url: str, dz: "Deezer | None" = None, settings: dict | None = None, work_dir: Path | None = None, user: str | None = None) -> "DispatchResult":
    """Route a download request: POST to the running server if up, else run
    locally via the core lib.

    Returns a DispatchResult tagged with `routed` = "server" or "local" (and
    `port`/`job_id` in server mode) so the CLI can tell the user which path it
    took. Raises nothing for the local path's auth/parse errors -- those are
    already handled inside run_playlist, which returns an error DispatchResult.
    """
    from dataclasses import replace
    from .downloader import DispatchResult

    port = server_is_up()
    if port is not None:
        r = _post_to_server(port, url, user=user)
        return replace(r, routed="server", port=port)
    # local fallback
    if dz is None or settings is None:
        raise RuntimeError("cli_dispatch local mode requires dz + settings")
    from .downloader import run_playlist
    r = run_playlist(url, dz, settings, work_dir=work_dir)
    return replace(r, routed="local")


def _post_to_server(port: int, url: str, user: str | None = None) -> "DispatchResult":
    """POST {url} to the running server's /download endpoint. Returns a
    DispatchResult -- success carries job_id (the server now sets ok=True),
    network/HTTP errors surface as an error DispatchResult so the CLI still
    reports cleanly."""
    import urllib.request
    import urllib.error
    from .downloader import DispatchResult

    token = _server_token()
    body = json.dumps({"url": url}).encode("utf-8")
    qs = urlencode({"user": user}) if user else ""
    req_url = f"http://{_bind_host()}:{port}/download?{qs}"
    req = urllib.request.Request(
        req_url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    if token:
        req.add_header("X-Auth-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.loads(r.read().decode("utf-8"))
            # The server returns {"job_id": ...} on success; normalize so the
            # single ok flag is always present (this was the missing-ok bug).
            if "ok" not in data:
                data["ok"] = True
            return DispatchResult(**data)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        return DispatchResult(ok=False, error=f"server returned HTTP {e.code}: {detail}")
    except Exception as e:
        return DispatchResult(ok=False, error=f"could not reach server on port {port}: {e}")
