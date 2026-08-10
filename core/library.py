"""Filesystem-as-registry helpers: skip-if-exists, tag+rename, meta write.

All three are pure filesystem operations on the playlist folder (out_dir).
No network, no session. Importable by app.py, sync.py, and the CLI alike.
"""
import re
from pathlib import Path

from mutagen.flac import FLAC


# ---------------------------------------------------------------------------
# skip-if-exists
# ---------------------------------------------------------------------------

def _normalize(s):
    """Case/space/punctuation-insensitive key for fuzzy track matching."""
    return re.sub(r'[^a-z0-9]+', '', (s or "").lower())


def _strip_position_prefix(stem):
    """Remove ALL leading 'NN - ' position prefixes, returning the core name.

    '05 - 13 - Eddie C - X' -> 'Eddie C - X'
    '13 - Eddie C - X'      -> 'Eddie C - X'
    'Eddie C - X'           -> 'Eddie C - X'
    """
    prev = None
    cur = stem.strip()
    while cur != prev:
        prev = cur
        cur = re.sub(r'^\d+\s*-\s+', '', cur).strip()
    return cur


def _core_artist_title(stem):
    """From a filename stem, return (artist, title) by splitting on ' - '
    after stripping any leading position prefix."""
    core = _strip_position_prefix(stem)
    if " - " in core:
        artist, _, title = core.partition(" - ")
        return artist.strip(), title.strip()
    return "", core


def find_existing_track(out_dir, artists, title):
    """Return an existing FLAC in out_dir whose TITLE matches the given track,
    or None. Match rule: normalized Spotify title must be contained in (or
    contain) the on-disk title. This tolerates the fact that Deezer filenames
    append mix/edition suffixes -- e.g. Spotify 'Adapt 2' vs the file
    'Adapt 2 (Original Mix)' -- so a strict-equality check would miss it and
    re-download every time. We match on TITLE ONLY (not artist): Spotify and
    Deezer routinely catalog the same track under different artist strings."""
    want_title = _normalize(title)
    if not want_title:
        return None
    for f in out_dir.glob("*.flac"):
        _, ftitle = _core_artist_title(f.stem)
        fnt = _normalize(ftitle)
        if fnt and (want_title in fnt or fnt in want_title):
            return f
    return None


# ---------------------------------------------------------------------------
# tag + rename
# ---------------------------------------------------------------------------

def tag_and_rename(flac_path, position, total):
    """Rename <name>.flac -> 'NN - <name>.flac' and set the Vorbis TRACKNUMBER
    comment to the Spotify playlist position (NN). FLAC uses Vorbis comments,
    NOT the ID3 'TRCK' key -- Windows Explorer's '#' column and the Denon Prime
    4 both read TRACKNUMBER, so we write that.

    Idempotent: strips any existing leading 'NN - ' first, so re-runs never
    produce a doubled prefix like '05 - 13 - Eddie C - X'.
    """
    core = _strip_position_prefix(flac_path.stem)
    target_name = f"{position:0{len(str(total))}d} - {core}.flac"
    # already correctly named? just ensure the tag is right
    if flac_path.name == target_name:
        try:
            audio = FLAC(str(flac_path))
            audio["TRACKNUMBER"] = f"{position:0{len(str(total))}d}"
            audio.save()
        except Exception:
            pass
        return flac_path
    new_path = flac_path.with_name(target_name)
    # If a correctly-named file already exists, the track is already present
    # and the freshly downloaded flac_path is a redundant duplicate (e.g. a
    # re-run the skip-check missed because Spotify and Deezer label the track
    # slightly differently). Delete the duplicate so we never leave an
    # unprefixed orphan in the folder, and keep the existing prefixed truth.
    if new_path.exists() and new_path != flac_path:
        try:
            flac_path.unlink()
        except Exception:
            pass  # if we can't delete, just leave the existing file as-is
        return new_path
    flac_path.rename(new_path)
    try:
        audio = FLAC(str(new_path))
        audio["TRACKNUMBER"] = f"{position:0{len(str(total))}d}"
        audio.save()
    except Exception:
        pass  # tag best-effort; rename already done
    return new_path


# ---------------------------------------------------------------------------
# meta write
# ---------------------------------------------------------------------------

def write_meta(out_dir, spotify_url, spotify_id, name, tracks, statuses):
    """Write playlist.meta.json into the playlist folder so a future sync cron
    can re-query Spotify and download only tracks not already fetched.

    Records the source URL/ID, playlist name, fetch time, and per-track:
    position, spotify_uri (stable key), artist, title, status
    (downloaded or missed). status comes from the parallel statuses list
    (same order as tracks).
    """
    import time as _time
    out_dir.mkdir(parents=True, exist_ok=True)
    meta = {
        "spotify_url": spotify_url,
        "spotify_id": spotify_id,
        "name": name,
        "fetched_at": _time.strftime("%Y-%m-%dT%H:%M:%S"),
        "tracks": [],
    }
    for t, st in zip(tracks, statuses):
        meta["tracks"].append({
            "position": t.get("position"),
            "spotify_uri": t.get("spotify_uri"),
            "artist": " ".join(t.get("artists", [])),
            "title": t.get("name"),
            "status": st,
        })
    path = out_dir / "playlist.meta.json"
    path.write_text(
        __import__("json").dumps(meta, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return path
