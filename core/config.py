"""Shared paths, config loading, and output-dir resolution.

Kept dependency-free (stdlib only) so every other core module can import it
without triggering heavy third-party imports (deezer/deemix/rich).
"""
import os
import sys
import re
from pathlib import Path

# ---- paths -----------------------------------------------------------------
# config.py lives in <repo>/core/config.py, so the repo root is its parent.
CORE = Path(__file__).resolve().parent
REPO = CORE.parent
DEFAULT_WORK_DIR = Path.home() / "Music" / "music_downloader_outputs"
CONF_SETTINGS = REPO / "config" / "settings.conf"
ARL = REPO / "deezer.arl"
DEEMIX_ARL = REPO / "config" / ".arl"
SPOTIFY_TOKEN_CACHE = REPO / "spotify_token.json"
SPOTIFY_REDIRECT = "http://127.0.0.1:48721/callback"


def die(msg):
    print(f"[fatal] {msg}", file=sys.stderr)
    sys.exit(1)


def read_conf(path):
    """Parse a simple 'key = value' conf into a dict."""
    d = {}
    if not path.is_file():
        return d
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        d[k.strip()] = v.strip()
    return d


def resolve_output_dir():
    """Output base dir precedence: env MUSIC_DOWNLOADER_OUT > config/settings.conf
    `output_dir` > default ~/Music/music_downloader_outputs. `~` expands to home.
    """
    env = os.environ.get("MUSIC_DOWNLOADER_OUT")
    if env:
        return Path(os.path.expanduser(env)).resolve()
    cfg = read_conf(CONF_SETTINGS)
    if cfg.get("output_dir"):
        return Path(os.path.expanduser(cfg["output_dir"])).resolve()
    return DEFAULT_WORK_DIR.resolve()


def sync_deezer_arl():
    """Make deezer.arl the single source of truth: copy it into config/.arl
    (deemix portable mode reads that). Keeps you from editing two files."""
    if not ARL.is_file():
        return
    try:
        DEEMIX_ARL.parent.mkdir(parents=True, exist_ok=True)
        DEEMIX_ARL.write_text(ARL.read_text(encoding="utf-8"), encoding="utf-8")
    except Exception:
        pass  # deemix will just use whatever is there
