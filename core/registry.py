"""Registry reader: walk the filesystem music library and summarize playlists.

Your rule is filesystem-as-registry -- each playlist folder under the output
root holds a playlist.meta.json (written by core.library.write_meta). This
helper reads those files into a JSON-friendly list for GET /playlists and the
future sync.py. No external state, no shadow DB.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import TypedDict

from .config import resolve_output_dir
from . import log


class PlaylistSummary(TypedDict):
    folder: str
    name: str
    spotify_url: str
    fetched_at: str
    total: int
    downloaded: int
    missed: int
    user: str


def list_playlists(work_dir: Path | None = None) -> list[PlaylistSummary]:
    """Return a list of dicts, one per folder containing playlist.meta.json.

    Each entry: folder, name, spotify_url, fetched_at, total, downloaded,
    missed. Sorted by folder name. Used by the web UI list and sync cron.
    """
    if work_dir is None:
        work_dir = resolve_output_dir()
    out = []
    base = Path(work_dir).resolve()
    for meta in sorted(base.rglob("playlist.meta.json")):
        folder = meta.parent.name
        try:
            text = meta.read_text(encoding="utf-8")
        except OSError as e:
            log.warning("[registry] skipping %s: cannot read meta: %s", folder, e)
            continue
        if not text.strip():
            log.warning("[registry] skipping %s: playlist.meta.json is empty "
                        "(download likely interrupted before the meta write)", folder)
            continue
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            log.warning("[registry] skipping %s: corrupt playlist.meta.json: %s", folder, e)
            continue
        # The owning user is the first path segment under the output root:
        # layout is WORK_DIR_BASE/<user>/<folder>. Deriving it from the meta
        # file's real location means sync re-enqueues each playlist under the
        # same user it was originally downloaded from (see enqueue_sync_all).
        user = None
        try:
            rel = meta.parent.resolve().relative_to(base)
            if len(rel.parts) >= 2:
                user = rel.parts[0]
        except Exception:
            pass
        tracks = data.get("tracks", []) or []
        downloaded = sum(1 for t in tracks if t.get("status") == "downloaded")
        missed = sum(1 for t in tracks if t.get("status") == "missed")
        out.append({
            "folder": folder,
            "user": user,
            "name": data.get("name", folder),
            "spotify_url": data.get("spotify_url"),
            "fetched_at": data.get("fetched_at"),
            "total": len(tracks),
            "downloaded": downloaded,
            "missed": missed,
        })
    return out
