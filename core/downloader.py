"""Orchestrator: download a Spotify playlist/album/track URL via Deezer/FLAC.

This is the importable workhorse used by cli.py, sync.py, and app.py.

    from core.downloader import run_playlist
    result = run_playlist(url, dz, settings, work_dir)

Returns a dict:
    {
        "ok": bool,
        "name": str,            # playlist/album name
        "folder": Path,         # output folder
        "downloaded": int,      # new FLACs written
        "skipped": int,         # already present
        "missed": int,          # no Deezer match
        "failed": int,          # download failed after match
        "missed_tracks": [...], # list of {name, artists} for reporting
    }
"""
from __future__ import annotations

import logging
from pathlib import Path
import time

from .config import resolve_output_dir, sync_deezer_arl, read_conf
from .spotify import safe_folder_name, validate_spotify_url, get_spotify_token, parse_spotify_playlist
from .deezer import deezer_search, deemix_download
from .library import find_existing_track, find_partial_track, tag_and_rename, write_meta
from .track import Track
from .event_types import JobEventType, DownloadStatus

log = logging.getLogger("music_downloader")


def run_playlist(url, dz, settings, work_dir=None, on_progress=None, on_event=None):
    """Download one Spotify playlist/album/track. Returns a result dict.

    Parameters
    ----------
    url : str
        Spotify URL (playlist, album, or track).
    dz : Deezer session (already logged in via ARL).
    settings : dict
        deemix settings (already loaded from config/).
    work_dir : Path | None
        Output root. Defaults to resolve_output_dir().
    on_progress : callable(msg) | None
        Optional text progress hook (receives each status line). When None,
        prints to stdout (CLI mode). When set (web/cron), caller decides
        where output goes.
    on_event : callable(dict) | None
        Optional structured event hook. Events are typed dicts:
        - {"type": "tracks", "total": T, "items": [{"pos": N, "name": "..."}, ...]}
        - {"type": "start", "pos": N, "name": "Artist - Title"}
        - {"type": "pct", "pct": 0-100}
        - {"type": "done", "pos": N, "status": "downloaded"|"skipped"|"missed"|"failed", "pct": 0|100}
    """
    out = {}
    def report(msg):
        if msg:
            log.info(msg)
        if on_progress:
            on_progress(msg)
        else:
            print(msg)
    def event(e):
        if on_event:
            on_event(e)

    if work_dir is None:
        work_dir = resolve_output_dir()

    pid = validate_spotify_url(url)
    if not pid:
        msg = "[!] Not a valid open.spotify.com playlist/album/track URL."
        report(msg)
        out.update(ok=False, error=msg)
        return out

    try:
        token = get_spotify_token()
    except SystemExit:
        raise
    except Exception as e:
        msg = f"[!] Spotify auth failed: {e}"
        report(msg)
        out.update(ok=False, error=msg)
        return out

    try:
        parsed = parse_spotify_playlist(token, pid)
        tracks = parsed["tracks"]
        pl_name = parsed["name"]
    except Exception as e:
        msg = f"[!] Playlist parse failed: {e}"
        report(msg)
        out.update(ok=False, error=msg)
        return out

    if not tracks:
        msg = f"[!] No tracks found in playlist '{pl_name}' (id={pid}) -- the playlist may be empty or inaccessible"
        report(msg)
        out.update(ok=False, error=msg)
        return out

    total = len(tracks)
    folder = safe_folder_name(pl_name) or pid
    out_dir = work_dir / folder

    # emit full track list upfront so the UI can render all rows immediately
    event({
        "type": JobEventType.TRACKS,
        "total": total,
        "items": [
            {"pos": t.position, "name": f"{' - '.join(t.artists)} - {t.name}" if t.artists else t.name}
            for t in tracks
        ],
    })

    report(f"[*] {total} tracks. Downloading FLAC from Deezer...")
    report(f"[*] output folder: {out_dir}")

    missed = []
    skipped = 0
    downloaded = 0
    failed = 0
    statuses = []

    for i, t in enumerate(tracks, 1):
        # Search Deezer using only the FIRST listed artist. Spotify often lists
        # remixers/featurees as additional artists (e.g. "Christoph Faust, BLANKA
        # (ES)"), and concatenating every artist into the query over-specifies it
        # -- Deezer returns 0 results. The scorer still ranks candidates against
        # ALL artists via target_artists, so we don't lose disambiguation; we
        # just send Deezer a query it can actually find.
        lead_artist = t.artists[0] if t.artists else ""
        q = f"{lead_artist} {t.name}".strip()
        artist_part = " - ".join(t.artists)
        if artist_part:
            display = f"{artist_part} - {t.name}"
        else:
            display = t.name
        pos = t.position
        label = f"[{pos}/{total}] {display[:60]}"

        event({"type": JobEventType.START, "pos": pos, "name": display[:60]})

        # check skip
        existing = find_existing_track(out_dir, t.artists, t.name)
        if existing:
            report(f"[{pos}/{total}] {display[:60]}")
            report(f"    [skip] already present: {existing.name}")
            event({"type": JobEventType.DONE, "pos": pos, "status": DownloadStatus.SKIPPED, "pct": 100})
            statuses.append("downloaded")
            skipped += 1
            continue
        # clean up any partial/interrupted leftover for this track BEFORE
        # downloading. deemix sees a same-named file on disk and treats it as
        # alreadyDownloaded, so the partial would otherwise block the real
        # download forever (track stuck as 'failed').
        # SAFETY: find_partial_track only matches INCOMPLETE FLACs (missing
        # TITLE/ALBUM tag = interrupted download). A fully-downloaded track that
        # was removed from the Spotify playlist is NEVER touched here -- sync is
        # strictly additive and does not reflect Spotify removals on disk.
        partial = find_partial_track(out_dir, t.artists, t.name)
        if partial:
            report(f"[{pos}/{total}] {display[:60]}")
            report(f"    [cleanup] removing partial download: {partial.name}")
            try:
                partial.unlink()
            except OSError as e:
                report(f"    [warn] could not remove partial {partial.name}: {e}")

        # search
        report(f"[{pos}/{total}] {display[:60]}")
        hit = deezer_search(dz, q, target_title=t.name, target_artists=t.artists)
        if not hit:
            # Fallback: strip edition suffixes from the query, and search by the
            # lead artist only (same reasoning as the primary query above).
            from .deezer import _strip_editions
            lead_artist = t.artists[0] if t.artists else ""
            q_fb = f"{lead_artist} {_strip_editions(t.name)}".strip()
            if q_fb != q:
                report(f"    [deezer] fallback search: {q_fb}")
                hit = deezer_search(dz, q_fb, target_title=t.name, target_artists=t.artists)
        if not hit:
            report("    [deezer] no match")
            event({"type": JobEventType.DONE, "pos": pos, "status": DownloadStatus.MISSED, "pct": 0})
            missed.append({"name": t.name, "artists": t.artists})
            statuses.append("missed")
            continue
        dz_url = hit.get("link")
        if not dz_url:
            missed.append({"name": t.name, "artists": t.artists})
            event({"type": JobEventType.DONE, "pos": pos, "status": DownloadStatus.MISSED, "pct": 0})
            statuses.append("missed")
            continue

        # download
        def pct_hook(p):
            event({"type": JobEventType.PCT, "pos": pos, "pct": p})

        flac = deemix_download(dz, dz_url, settings, out_dir, label,
                               on_progress=on_progress, on_pct=pct_hook if on_event else None)
        if flac:
            final = tag_and_rename(flac, pos, total)
            report(f"    [deezer] FLAC downloaded -> {final.name}")
            event({"type": JobEventType.DONE, "pos": pos, "status": DownloadStatus.DOWNLOADED, "pct": 100})
            statuses.append("downloaded")
            downloaded += 1
        else:
            report("    [deezer] download failed")
            event({"type": JobEventType.DONE, "pos": pos, "status": DownloadStatus.FAILED, "pct": 0})
            missed.append({"name": t.name, "artists": t.artists})
            statuses.append("missed")
            failed += 1

    try:
        meta_path = write_meta(out_dir, url, pid, pl_name, tracks, statuses)
        report(f"[*] wrote {meta_path.name}")
    except Exception as e:
        report(f"[warn] could not write playlist.meta.json: {e}")

    if missed:
        report(f"\n[!] {len(missed)} tracks unmatched by Deezer (see status in playlist.meta.json):")
        for mt in missed:
            report("   - " + " ".join(mt["artists"]) + " " + mt["name"])
    report("")

    return {
        "ok": True,
        "name": pl_name,
        "folder": out_dir,
        "downloaded": downloaded,
        "skipped": skipped,
        "missed": len(missed),
        "failed": failed,
        "missed_tracks": missed,
    }
