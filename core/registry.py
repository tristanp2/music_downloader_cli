"""Registry reader: walk the filesystem music library and summarize playlists.

Your rule is filesystem-as-registry -- each playlist folder under the output
root holds a playlist.meta.json (written by core.library.write_meta). This
helper reads those files into a JSON-friendly list for GET /playlists and the
future sync.py. No external state, no shadow DB.
"""
import json
from pathlib import Path

from .config import resolve_output_dir


def list_playlists(work_dir=None):
    """Return a list of dicts, one per folder containing playlist.meta.json.

    Each entry: folder, name, spotify_url, fetched_at, total, downloaded,
    missed. Sorted by folder name. Used by the web UI list and sync cron.
    """
    if work_dir is None:
        work_dir = resolve_output_dir()
    out = []
    for meta in sorted(work_dir.rglob("playlist.meta.json")):
        try:
            data = json.loads(meta.read_text(encoding="utf-8"))
        except Exception:
            continue
        folder = meta.parent.name
        tracks = data.get("tracks", []) or []
        downloaded = sum(1 for t in tracks if t.get("status") == "downloaded")
        missed = sum(1 for t in tracks if t.get("status") == "missed")
        out.append({
            "folder": folder,
            "name": data.get("name", folder),
            "spotify_url": data.get("spotify_url"),
            "fetched_at": data.get("fetched_at"),
            "total": len(tracks),
            "downloaded": downloaded,
            "missed": missed,
        })
    return out
