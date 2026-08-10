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

from pathlib import Path
import time

from .config import resolve_output_dir, sync_deezer_arl, read_conf
from .spotify import safe_folder_name, validate_spotify_url, get_spotify_token, parse_spotify_playlist
from .deezer import deezer_search, deemix_download
from .library import find_existing_track, tag_and_rename, write_meta


def run_playlist(url, dz, settings, work_dir=None, on_progress=None):
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
        Optional progress hook (receives each status line). When None, prints
        to stdout (CLI mode). When set (web/cron), caller decides where output
        goes.
    """
    out = {}
    def report(msg):
        if on_progress:
            on_progress(msg)
        else:
            print(msg)

    if work_dir is None:
        work_dir = resolve_output_dir()

    pid = validate_spotify_url(url)
    if not pid:
        out.update(ok=False, error="not a valid open.spotify.com playlist/album/track URL.")
        return out

    try:
        token = get_spotify_token()
    except SystemExit:
        raise
    except Exception as e:
        out.update(ok=False, error=f"Spotify auth failed: {e}")
        return out

    try:
        parsed = parse_spotify_playlist(token, pid)
        tracks = parsed["tracks"]
        pl_name = parsed["name"]
    except Exception as e:
        out.update(ok=False, error=f"playlist parse failed: {e}")
        return out

    if not tracks:
        out.update(ok=True, name=pl_name, folder=Path(""), downloaded=0, skipped=0,
                   missed=0, failed=0, missed_tracks=[])
        return out

    folder = safe_folder_name(pl_name) or pid
    out_dir = work_dir / folder

    report(f"[*] {len(tracks)} tracks. Downloading FLAC from Deezer...")
    report(f"[*] output folder: {out_dir}")

    missed = []
    skipped = 0
    downloaded = 0
    failed = 0
    statuses = []

    for i, t in enumerate(tracks, 1):
        q = f"{' '.join(t['artists'])} {t['name']}"
        artist_part = " - ".join(t["artists"])
        if artist_part:
            display = f"{artist_part} - {t['name']}"
        else:
            display = t["name"]
        pos = t["position"]
        label = f"[{pos}/{len(tracks)}] {display[:60]}"
        report(label)

        total = len(tracks)
        nn = f"{pos:0{len(str(total))}d}"
        existing = find_existing_track(out_dir, t["artists"], t["name"])
        if existing:
            report(f"    [skip] already present: {existing.name}")
            statuses.append("downloaded")
            skipped += 1
            continue

        hit = deezer_search(dz, q, target_title=t["name"], target_artists=t["artists"])
        if not hit:
            report("    [deezer] no match")
            missed.append({"name": t["name"], "artists": t["artists"]})
            statuses.append("missed")
            continue
        dz_url = hit.get("link")
        if not dz_url:
            missed.append({"name": t["name"], "artists": t["artists"]})
            statuses.append("missed")
            continue

        flac = deemix_download(dz, dz_url, settings, out_dir, label, on_progress=on_progress)
        if flac:
            final = tag_and_rename(flac, pos, len(tracks))
            report(f"    [deezer] FLAC downloaded -> {final.name}")
            statuses.append("downloaded")
            downloaded += 1
        else:
            report("    [deezer] download failed")
            missed.append({"name": t["name"], "artists": t["artists"]})
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
