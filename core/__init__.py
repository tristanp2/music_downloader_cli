"""core package -- importable pipeline for Spotify -> Deezer FLAC.

Public API (re-exported here for convenience):

    from core import run_playlist, validate_spotify_url
    from core.config import resolve_output_dir
    from core.deezer import init_deezer, deezer_search, deemix_download
    from core.library import find_existing_track, tag_and_rename, write_meta
    from core.spotify import get_spotify_token, get_spotify_token_silent, parse_spotify_playlist
"""
import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import read_conf, CONF_SETTINGS

REPO = Path(__file__).resolve().parent.parent
LOG_DIR = REPO / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Log rotation config: capped size + N backup files, old logs roll off.
# Precedence: env MUSIC_DOWNLOADER_LOG_MAX_BYTES / MUSIC_DOWNLOADER_LOG_BACKUPS
# > config/settings.conf (log_max_bytes / log_backups) > built-in defaults.
# ---------------------------------------------------------------------------

def _log_int(key_conf, key_env, default):
    v = os.environ.get(key_env)
    if v is None:
        v = read_conf(CONF_SETTINGS).get(key_conf)
    if v is None:
        return default
    try:
        return int(v)
    except ValueError:
        return default

MAX_BYTES = _log_int("log_max_bytes", "MUSIC_DOWNLOADER_LOG_MAX_BYTES", 5 * 1024 * 1024)
BACKUPS = _log_int("log_backups", "MUSIC_DOWNLOADER_LOG_BACKUPS", 5)


class _HealthAccessFilter(logging.Filter):
    """Drop uvicorn.access lines for the 10s /health heartbeat poll so they
    don't clutter the log. Every other request still logs normally.
    """

    def filter(self, record):
        return "/health" not in record.getMessage()

# ---------------------------------------------------------------------------
# Our logger: console + rotating file.  Uvicorn's loggers are wired into this
# at server startup (app.py) via attach_uvicorn_loggers().
# ---------------------------------------------------------------------------

logger = logging.getLogger("music_downloader")
logger.setLevel(logging.DEBUG)

_console = logging.StreamHandler()
_console.setLevel(logging.INFO)
_console.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)-5s %(message)s", datefmt="%H:%M:%S"))
logger.addHandler(_console)

_file_path = str(LOG_DIR / "music_downloader.log")
_fh = RotatingFileHandler(_file_path, maxBytes=MAX_BYTES, backupCount=BACKUPS, encoding="utf-8")
_fh.setLevel(logging.DEBUG)
_fh.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)-5s %(name)s:%(lineno)d %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
logger.addHandler(_fh)

# Exported alias
log = logger


def attach_uvicorn_loggers():
    """Called at server startup: replace uvicorn's log handlers with ours.

    uvicorn / uvicorn.error and uvicorn.access get console + file handlers, so
    every request (except /health) is recorded on disk. A _HealthAccessFilter
    drops /health entirely -- the 10s heartbeat poll is logged nowhere.
    Errors still reach the file via uvicorn.error.
    """
    import logging as _logging
    for name in ("uvicorn", "uvicorn.error"):
        uv = _logging.getLogger(name)
        uv.handlers = []
        uv.addHandler(_console)
        uv.addHandler(_fh)
        uv.setLevel(logging.DEBUG)
        uv.propagate = False
    # access logging: console + file for every request, EXCEPT /health which
    # is dropped entirely by _HealthAccessFilter (nowhere -- not console, not file).
    uv = _logging.getLogger("uvicorn.access")
    uv.handlers = []
    uv.addHandler(_console)
    uv.addHandler(_fh)
    uv.setLevel(logging.INFO)
    uv.propagate = False
    uv.addFilter(_HealthAccessFilter())


def _log_from_prints(logger_ref):
    """Convenience: return the logger so modules can use `log.info(msg)` etc.

    Old code that does `print(msg)` or `on_progress(msg)` should call
    `log.info(msg)` or `log.debug(msg)` instead. The on_progress callback
    is kept for backwards compat but new code should use the logger directly.
    """
    return logger_ref


from .downloader import run_playlist
from .spotify import (
    get_spotify_token,
    get_spotify_token_silent,
    parse_spotify_playlist,
    validate_spotify_url,
    safe_folder_name,
)
from .deezer import init_deezer, deezer_search, deemix_download
from .library import find_existing_track, tag_and_rename, write_meta
from .track import Track
from .job import Job, JobProgress, TrackState
from .event_types import JobEventType, DownloadStatus
from .config import resolve_output_dir, sync_deezer_arl
from . import server_lock

__all__ = [
    "run_playlist",
    "get_spotify_token",
    "get_spotify_token_silent",
    "parse_spotify_playlist",
    "validate_spotify_url",
    "safe_folder_name",
    "init_deezer",
    "deezer_search",
    "deemix_download",
    "find_existing_track",
    "tag_and_rename",
    "write_meta",
    "Track",
    "Job",
    "JobProgress",
    "TrackState",
    "JobEventType",
    "DownloadStatus",
    "resolve_output_dir",
    "sync_deezer_arl",
    "read_conf",
    "server_lock",
    "log",
]
