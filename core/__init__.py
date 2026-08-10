"""core package -- importable pipeline for Spotify -> Deezer FLAC.

Public API (re-exported here for convenience):

    from core import run_playlist, validate_spotify_url
    from core.config import resolve_output_dir
    from core.deezer import init_deezer, deezer_search, deemix_download
    from core.library import find_existing_track, tag_and_rename, write_meta
    from core.spotify import get_spotify_token, get_spotify_token_silent, parse_spotify_playlist
"""
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
    "resolve_output_dir",
    "sync_deezer_arl",
    "read_conf",
    "server_lock",
]
