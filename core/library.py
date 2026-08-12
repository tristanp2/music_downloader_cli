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


def _core_artist_title(stem):
    """From a filename stem, return (artist, title) by splitting on ' - '.

    Filenames are bare (no position prefix), e.g. 'Eddie C - X'.
    """
    if " - " in stem:
        artist, _, title = stem.partition(" - ")
        return artist.strip(), title.strip()
    return "", stem


def _is_complete_flac(path):
    """Return True only if the file is a valid FLAC whose core metadata
    (TITLE + ALBUM, the fields Windows Explorer shows in its columns) is
    actually populated.

    deemix writes a Vorbis-comment block up front, so a partial/interrupted
    download still parses and may even carry a placeholder tag -- but its
    TITLE/ALBUM/ARTIST are blank. That is exactly what makes Explorer show
    empty columns for a half-written file. Requiring real TITLE + ALBUM
    values is therefore the reliable "is this download finished" signal.
    """
    if not path.is_file():
        return False
    try:
        audio = FLAC(str(path))
        if not audio.tags:
            return False
        # Case-insensitive lookup: mutagen stores both uppercase (Vorbis
        # spec) and lowercase aliases, but be safe about it.
        def has(field):
            for key in (field, field.lower(), field.upper()):
                v = audio.tags.get(key)
                if v:
                    return True
            return False
        return has("TITLE") and has("ALBUM")
    except Exception:
        return False


def _title_matches(flac_path, title):
    """Return True if the on-disk FLAC's TITLE (parsed from the filename) is a
    fuzzy match for the requested Spotify title. Match rule: normalized Spotify
    title must be contained in (or contain) the on-disk title. This tolerates
    Deezer filename suffixes like ' (Original Mix)'. Title-only (not artist):
    Spotify and Deezer routinely catalog the same track under different artist
    strings, so matching on artist would cause false negatives.
    """
    want = _normalize(title)
    if not want:
        return False
    _, ftitle = _core_artist_title(flac_path.stem)
    fnt = _normalize(ftitle)
    return bool(fnt and (want in fnt or fnt in want))


def find_existing_track(out_dir, artists, title):
    """Return an existing COMPLETE FLAC in out_dir matching the given track,
    or None.

    A file is considered a match only if it is a valid, fully-tagged FLAC
    (see _is_complete_flac) -- partial/interrupted downloads are ignored so
    they get re-fetched instead of being treated as already present.
    """
    if not _normalize(title):
        return None
    for f in out_dir.glob("*.flac"):
        if not _is_complete_flac(f):
            continue
        if _title_matches(f, title):
            return f
    return None


def find_partial_track(out_dir, artists, title):
    """Return a PARTIAL (incomplete) FLAC in out_dir matching the given track,
    or None.

    Used to clean up interrupted downloads before re-downloading: deemix will
    see a same-named file on disk and skip the write (treating it as
    alreadyDownloaded), so the leftover partial must be deleted first or the
    track is perpetually stuck as 'failed'.
    """
    if not _normalize(title):
        return None
    for f in out_dir.glob("*.flac"):
        if _is_complete_flac(f):
            continue
        if _title_matches(f, title):
            return f
    return None


# ---------------------------------------------------------------------------
# tag + rename
# ---------------------------------------------------------------------------

def tag_and_rename(flac_path, position, total):
    """Set the Vorbis TRACKNUMBER comment to the Spotify playlist position
    (NN) and ensure the filename is the bare core name.

    FLAC uses Vorbis comments, NOT the ID3 'TRCK' key -- Windows Explorer's
    '#' column and the Denon Prime 4 both read TRACKNUMBER, so we write that
    for playlist ordering. The position is carried in the tag, not the
    filename, so the on-disk name stays clean (e.g. 'Eddie C - X.flac').
    """
    core = flac_path.stem
    target_name = f"{core}.flac"
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
    # slightly differently). Delete the duplicate so we never leave an orphan
    # in the folder, and keep the existing truth.
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
