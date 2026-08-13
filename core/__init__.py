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
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LOG_DIR = REPO / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Our logger: console + file.  Uvicorn's loggers are wired into this at
# server startup (app.py) by adding a FileHandler to them.
# ---------------------------------------------------------------------------

logger = logging.getLogger("musicdl")
logger.setLevel(logging.DEBUG)

_console = logging.StreamHandler()
_console.setLevel(logging.INFO)
_console.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)-5s %(message)s", datefmt="%H:%M:%S"))
logger.addHandler(_console)

_file_path = str(LOG_DIR / "musicdl.log")
_fh = logging.FileHandler(_file_path, encoding="utf-8")
_fh.setLevel(logging.DEBUG)
_fh.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)-5s %(name)s:%(lineno)d %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
logger.addHandler(_fh)

# Exported alias
log = logger


def attach_uvicorn_loggers():
    """Called at server startup: replace uvicorn's log handlers with ours.

    Uvicorn's default loggers get cleared and pointed at our console + file
    handlers so all output (app + server) uses the same format.
    """
    import logging as _logging
    for name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        uv = _logging.getLogger(name)
        uv.handlers = []
        uv.addHandler(_console)
        uv.addHandler(_fh)
        uv.setLevel(logging.DEBUG)
        uv.propagate = False


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
from .config import resolve_output_dir, sync_deezer_arl, read_conf
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
