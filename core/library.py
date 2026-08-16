"""Filesystem-as-registry helpers: skip-if-exists, tag+rename, meta write.

All three are pure filesystem operations on the playlist folder (out_dir).
No network, no session. Importable by app.py, sync.py, and the CLI alike.
"""
import re
from pathlib import Path

from mutagen.flac import FLAC
from mutagen.easyid3 import EasyID3
from mutagen import File as mutagen_file

from .track import Track


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


def _is_complete_audio(path):
    """Return True only if the file is a valid audio file (FLAC or MP3)
    whose core metadata (TITLE + ALBUM, the fields Windows Explorer shows in
    its columns) is actually populated.

    Container-agnostic: uses mutagen.File to auto-detect FLAC vs MP3, so an
    MP3 fallback download is treated the same as a FLAC -- a complete MP3 is
    recognized as present and skipped on re-sync, not re-downloaded.

    deemix writes a tag block up front, so a partial/interrupted download
    still parses and may even carry a placeholder tag -- but its TITLE/ALBUM
    are blank. Requiring real TITLE + ALBUM values is the reliable "is this
    download finished" signal. Vorbis (FLAC) keys are TITLE/ALBUM; ID3 (MP3)
    keys are TIT2/TALB -- check both.
    """
    if not path.is_file():
        return False
    try:
        audio = mutagen_file(str(path))
        if audio is None:
            return False
        tags = audio.tags
        if not tags:
            return False

        def has(*keys):
            for k in keys:
                if tags.get(k):
                    return True
            return False

        return has("TITLE", "title", "TIT2") and has("ALBUM", "album", "TALB")
    except Exception:
        return False


def _title_matches(flac_path, title):
    """Return True if the on-disk FLAC's TITLE (parsed from the filename) is a
    match for the requested Spotify title, meaning "same recording -> skip".

    Match rule, in order:
      1. Exact normalized equality (case/punctuation-insensitive).
      2. Approved-edition match in EITHER direction: one title equals the other
         plus exactly a recognized SAME-RECORDING marker -- "(Original Mix)" or
         "(Extended Mix)". This tolerates the Spotify<->Deezer labeling
         inconsistency (one service carries the suffix, the other doesn't).

    Anything else is treated as a DIFFERENT recording and is NOT skipped --
    including any remix/version tail ("(Mystic State Remix)", "VIP Mix", "Dub",
    "Radio Edit"), because those are distinct tracks, not just labelled editions.
    This is what previously broke: the old substring-containment rule let
    "Seek & Move - Mystic State Remix" falsely match the base "Seek & Move".

    NOTE: duration-based fallback is intentionally NOT here yet -- see the
    planned tier 3. Title-only for now.
    """
    want = _normalize(title)
    if not want:
        return False
    _, ftitle = _core_artist_title(flac_path.stem)
    fnt = _normalize(ftitle)
    if not fnt:
        return False
    # 1. exact
    if want == fnt:
        return True
    # 2. approved-edition match, either direction
    return _same_recording_with_edition(want, fnt)


# Recognized SAME-RECORDING edition markers. Conservative on purpose: only
# markers that denote the identical audio under a different label. Remixes,
# VIPs, dubs, radio edits, etc. are deliberately excluded -- they are different
# recordings.
_APPROVED_EDITIONS = {"original mix", "extended mix", "extended",
                      "studio version", "studio"}


def _same_recording_with_edition(a, b):
    """True if `a` and `b` are identical except one carries an approved-edition
    suffix (in either order). The suffix must be a parenthesised/bounded marker
    from _APPROVED_EDITIONS, attached after the shared core title.

    Comparison is on normalized strings (see _normalize), so spaces/punctuation
    inside the edition marker don't matter -- "original mix" and "originalmix"
    match identically.
    """
    APPROVED = {_normalize(e) for e in _APPROVED_EDITIONS}
    # Check a == b + approved-suffix, then b == a + approved-suffix.
    for core, full in ((a, b), (b, a)):
        if not full.startswith(core):
            continue
        tail = full[len(core):]
        # strip a leading separator (space, dash, parenthesis, dot)
        tail = re.sub(r"^[\s\-(\.]+", "", tail).strip()
        if tail in APPROVED:
            return True
    return False


def find_existing_track(out_dir, artists, title):
    """Return an existing COMPLETE audio file (FLAC or MP3) in out_dir
    matching the given track, or None.

    A file is considered a match only if it is valid and fully tagged
    (see _is_complete_audio) -- partial/interrupted downloads are ignored so
    they get re-fetched instead of being treated as already present.
    """
    if not _normalize(title):
        return None
    for f in (*out_dir.glob("*.flac"), *out_dir.glob("*.mp3")):
        if not _is_complete_audio(f):
            continue
        if _title_matches(f, title):
            return f
    return None


def find_partial_track(out_dir, artists, title):
    """Return a PARTIAL (incomplete) audio file (FLAC or MP3) in out_dir
    matching the given track, or None.

    Used to clean up interrupted downloads before re-downloading: deemix will
    see a same-named file on disk and skip the write (treating it as
    alreadyDownloaded), so the leftover partial must be deleted first or the
    track is perpetually stuck as 'failed'.
    """
    if not _normalize(title):
        return None
    for f in (*out_dir.glob("*.flac"), *out_dir.glob("*.mp3")):
        if _is_complete_audio(f):
            continue
        if _title_matches(f, title):
            return f
    return None


# ---------------------------------------------------------------------------
# tag + rename
# ---------------------------------------------------------------------------

def tag_and_rename(flac_path, position, total):
    """Set the playlist position (NN) in the file's tag and ensure the
    filename is the bare core name, preserving the real audio extension.

    FLAC uses Vorbis comments (TRACKNUMBER); MP3 uses ID3 (TRCK). Windows
    Explorer's '#' column and the Denon Prime 4 both read these for playlist
    ordering. The position lives in the tag, not the filename, so the on-disk
    name stays clean (e.g. 'Eddie C - X.flac' or 'Eddie C - X.mp3'). The
    extension is taken from the file itself -- an MP3 must stay .mp3.
    """
    core = flac_path.stem
    target_name = f"{core}{flac_path.suffix}"
    # already correctly named? just ensure the tag is right
    if flac_path.name == target_name:
        try:
            _write_position(flac_path, position, total)
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
        _write_position(new_path, position, total)
    except Exception:
        pass  # tag best-effort; rename already done
    return new_path


def _write_position(path, position, total):
    """Write the playlist position into the tag using the correct container
    for the file type (Vorbis TRACKNUMBER for FLAC, ID3 TRCK for MP3)."""
    if path.suffix.lower() == ".mp3":
        audio = EasyID3(str(path))
        audio["tracknumber"] = f"{position:0{len(str(total))}d}"
        audio.save()
    else:
        audio = FLAC(str(path))
        audio["TRACKNUMBER"] = f"{position:0{len(str(total))}d}"
        audio.save()


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
            "position": t.position,
            "spotify_uri": t.spotify_uri,
            "artist": " ".join(t.artists),
            "title": t.name,
            "status": st,
        })
    path = out_dir / "playlist.meta.json"
    path.write_text(
        __import__("json").dumps(meta, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return path
