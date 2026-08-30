"""Filesystem-as-registry helpers: skip-if-exists, tag+rename, meta write.

All three are pure filesystem operations on the playlist folder (out_dir).
No network, no session. Importable by app.py, sync.py, and the CLI alike.
"""
from __future__ import annotations

import re
import threading
from pathlib import Path

from mutagen.flac import FLAC
from mutagen.easyid3 import EasyID3
from mutagen import File as mutagen_file

from .track import Track


# ---------------------------------------------------------------------------
# skip-if-exists
# ---------------------------------------------------------------------------

def _normalize(s: str | None) -> str:
    """Case/space/punctuation-insensitive key for fuzzy track matching."""
    return re.sub(r'[^a-z0-9]+', '', (s or "").lower())


def _core_artist_title(stem: str) -> tuple[str, str]:
    """From a filename stem, return (artist, title) by splitting on ' - '.

    Filenames are bare (no position prefix), e.g. 'Eddie C - X'.
    """
    if " - " in stem:
        artist, _, title = stem.partition(" - ")
        return artist.strip(), title.strip()
    return "", stem


# Windows-forbidden filename chars (and quote, which Explorer also rejects).
_ILLEGAL_FILENAME = re.compile(r'[:?*/\\<>|"]')


def _sanitize_filename_part(s: str) -> str:
    """Make a single name component safe for Windows paths.

    Replaces illegal chars with '_' and trims leading/trailing dots/spaces so we
    never produce 'K.O..flac' or '.hidden' style names. This mirrors what
    deemix/sockseek already do for on-disk names.
    """
    if not s:
        return ""
    cleaned = _ILLEGAL_FILENAME.sub("_", s).strip()
    return cleaned.strip(".").strip()


def format_canonical_name(artists: list[str], title: str) -> str:
    """Build the on-disk filename STEM from Spotify's structured data.

    This is the single source of truth for naming -- NOT the downloader's own
    output filename (deemix/sockseek name however they please). We take the
    primary artist (artists[0]) + the Spotify title, which is exactly the clean
    'Artist - Title' shape already used on disk, and sanitize it for Windows.

    Using only artists[0] (not the full joined list) deliberately avoids the
    bloated 'Axel Boman Kasper Bjørke' form Spotify's artist field carries when
    a remixer is listed as a second artist -- the remixer already lives in the
    title for those tracks. Returns '' if there is no usable title, signalling
    the caller to keep the existing stem rather than blank the name.
    """
    title_part = _sanitize_filename_part(title or "")
    if not title_part:
        return ""
    artist_part = _sanitize_filename_part(artists[0]) if artists else ""
    return f"{artist_part} - {title_part}" if artist_part else title_part


def _read_title_tag(path: Path) -> str | None:
    """Return the file's TITLE tag (FLAC Vorbis TITLE or MP3 TIT2), else None.

    Used by _title_matches so a Soulseek file whose FILENAME carries a redundant
    artist prefix still matches by its real, tagged title.
    """
    try:
        audio = mutagen_file(str(path))
        if not audio or not audio.tags:
            return None
        for k in ("TITLE", "title", "TIT2"):
            v = audio.tags.get(k)
            if v:
                return str(v[0]) if isinstance(v, list) else str(v)
    except Exception:
        return None
    return None


def _is_complete_audio(path: Path) -> bool:
    """Return True only if the file is a valid audio file (FLAC or MP3)
    whose core metadata (TITLE + ALBUM, the fields Windows Explorer shows in
    its columns) is actually populated.

    Container-agnostic: uses mutagen.File to auto-detect FLAC vs MP3, so an
    MP3 fallback download is treated the same as a FLAC -- a complete MP3 is
    recognized as present and skipped on re-sync, not re-downloaded.

    deemix writes a tag block up front, so a partial/interrupted download
    still parses and may even carry a placeholder tag -- but its TITLE/ALBUM
    are blank. Requiring real TITLE + (ALBUM or SOURCE) values is the reliable
    "is this download finished" signal. SOURCE is the dedicated download-source
    tag (FLAC Vorbis 'SOURCE' / MP3 TXXX 'SOURCE') -- its presence proves the
    file was fully tagged by our pipeline, so a missing ALBUM no longer means
    "partial". Vorbis (FLAC) keys are TITLE/ALBUM/SOURCE; ID3 (MP3) keys are
    TIT2/TALB/TXXX(SOURCE) -- check both.
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

        # MP3 stores SOURCE in a TXXX frame described 'SOURCE'
        def has_source():
            if has("SOURCE"):
                return True
            for k in (tags.keys() if hasattr(tags, "keys") else []):
                if str(k).upper().startswith("TXXX") and tags[k]:
                    val = tags[k]
                    desc = val[0] if isinstance(val, list) else val
                    if "SOURCE" in str(desc).upper():
                        return True
            return False

        return has("TITLE", "title", "TIT2") and (has("ALBUM", "album", "TALB") or has_source())
    except Exception:
        return False


def _title_matches(flac_path: Path, title: str) -> bool:
    """Return True if the on-disk file's title (filename stem OR actual TITLE
    tag) is a match for the requested Spotify title -- meaning "same recording
    -> skip".

    Match rule, in order, for BOTH the filename stem title and the TITLE tag:
      1. Exact normalized equality (case/punctuation-insensitive).
      2. Containment (either direction), when the shorter side is >= 4 chars.
         Handles Soulseek files whose title carries a redundant artist prefix
         (e.g. a track titled "In The Back" lands as "Jafu - In The Back", so
         neither the stem nor the tag equals "In The Back" -- but "In The Back"
         is contained in "Jafu - In The Back").
      3. Approved-edition match in EITHER direction: one title equals the other
         plus exactly a recognized SAME-RECORDING marker -- "(Original Mix)" or
         "(Extended Mix)". This tolerates the Spotify<->Deezer labeling
         inconsistency (one service carries the suffix, the other doesn't).

    A remix/version tail ("(Mystic State Remix)", "VIP Mix", "Dub", "Radio
    Edit") is NOT matched by containment (too short / wrong shape) and is
    treated as a DIFFERENT recording -- not skipped. This is what previously
    broke: the old substring rule let "Seek & Move - Mystic State Remix" falsely
    match the base "Seek & Move".

    NOTE: duration-based fallback is intentionally NOT here yet -- see the
    planned tier 4. Title-only (+ containment + editions) for now.
    """
    want = _normalize(title)
    if not want:
        return False

    def _same(a: str, b: str) -> bool:
        if not a or not b:
            return False
        if a == b:
            return True
        # containment, bounded so a short title can't swallow a long one
        if len(a) >= 4 and len(b) >= 4 and (a in b or b in a):
            return True
        return False

    # (a) filename stem title
    _, ftitle = _core_artist_title(flac_path.stem)
    if _same(want, _normalize(ftitle)):
        return True
    # (b) actual TITLE tag (Soulseek files may prefix the artist into the title)
    tag_title = _read_title_tag(flac_path)
    if tag_title and _same(want, _normalize(tag_title)):
        return True
    # (c) approved-edition match against the stem title
    return _same_recording_with_edition(want, _normalize(ftitle))


# Recognized SAME-RECORDING edition markers. Conservative on purpose: only
# markers that denote the identical audio under a different label. Remixes,
# VIPs, dubs, radio edits, etc. are deliberately excluded -- they are different
# recordings.
_APPROVED_EDITIONS = {"original mix", "extended mix", "extended",
                      "studio version", "studio"}


def _same_recording_with_edition(a: str, b: str) -> bool:
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


def find_existing_track(out_dir: Path, artists: list[str], title: str) -> Path | None:
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


def _index_present_tracks(out_dir: Path) -> list[tuple[Path, str]]:
    """Glob the folder ONCE and return [(path, normalized_title), ...] for every
    COMPLETE audio file. Parses each file's tags exactly once.

    Callers match tracks against this list instead of calling
    find_existing_track() per track -- that re-scans + re-parses the whole
    folder for every track, which is an O(tracks * files) tag-read storm that
    makes a large playlist's library read crawl. One pass here collapses it to
    O(files). The downstream match is the same _title_matches rule, just run
    in memory against the prebuilt index.
    """
    if not out_dir.is_dir():
        return []
    present_tracks: list[tuple[Path, str]] = []
    for audio_file in (*out_dir.glob("*.flac"), *out_dir.glob("*.mp3")):
        if not _is_complete_audio(audio_file):
            continue
        _, file_title = _core_artist_title(audio_file.stem)
        normalized_file_title = _normalize(file_title)
        # mirrors _title_matches: an empty title can never match
        if normalized_file_title:
            present_tracks.append((audio_file, normalized_file_title))
    return present_tracks


def find_existing_in_index(index: list[tuple[Path, str]], title: str) -> Path | None:
    """Match `title` against a prebuilt index from _index_present_tracks().

    Same match rule as find_existing_track (exact normalized OR approved-
    edition, either direction) but with zero disk I/O -- the index is scanned
    in memory. Returns the matched Path or None.
    """
    requested_title = _normalize(title)
    if not requested_title:
        return None
    for audio_file, normalized_file_title in index:
        if requested_title == normalized_file_title \
                or _same_recording_with_edition(requested_title, normalized_file_title):
            return audio_file
    return None


def find_partial_track(out_dir: Path, artists: list[str], title: str) -> Path | None:
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

def tag_and_rename(flac_path: Path, position: int, total: int, track: "Track | None" = None) -> Path:
    """Set the playlist position (NN) in the file's tag and ensure the
    filename is the canonical 'Artist - Title' name derived from Spotify's
    structured data (track.artists / track.name) -- NOT from whatever stem the
    downloader emitted.

    FLAC uses Vorbis comments (TRACKNUMBER); MP3 uses ID3 (TRCK). Windows
    Explorer's '#' column and the Denon Prime 4 both read these for playlist
    ordering. The position lives in the tag, not the filename. The extension is
    taken from the file itself -- an MP3 must stay .mp3.

    If `track` is unavailable we fall back to preserving the existing stem (the
    old behaviour) so a missing-metadata edge case can never blank a filename.
    """
    if track is not None:
        canonical = format_canonical_name(track.artists, track.name)
    else:
        canonical = ""
    core = canonical or flac_path.stem
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


def _write_position(path: Path, position: int, total: int) -> None:
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

# ---------------------------------------------------------------------------
# provenance lock
# ---------------------------------------------------------------------------
# One lock per playlist (keyed by the absolute folder path) so the Deezer drain
# and the parallel Soulseek sweep never do disk work on the SAME playlist at the
# SAME time. Cross-source disk work (writing FLACs / mutating playlist.meta.json)
# must be serialized per playlist; the Deezer session itself stays single-owner
# on its own drain thread and is untouched by this lock.
PLAYLIST_LOCKS: dict[Path, threading.Lock] = {}
PLAYLIST_LOCKS_LOCK = threading.Lock()


def playlist_lock(out_dir: Path) -> threading.Lock:
    """Return (creating lazily) the per-playlist lock for `out_dir`."""
    key = out_dir.resolve()
    with PLAYLIST_LOCKS_LOCK:
        lock = PLAYLIST_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            PLAYLIST_LOCKS[key] = lock
        return lock


def write_meta(out_dir: Path, spotify_url: str, spotify_id: str, name: str, tracks: list[Track], statuses: list[str], sources: dict[int, str] | None = None, provenance: dict[int, dict] | None = None) -> Path:
    """Write playlist.meta.json into the playlist folder so a future sync cron
    can re-query Spotify and download only tracks not already fetched.

    Records the source URL/ID, playlist name, fetch time, and per-track:
    position, spotify_uri (stable key), artist, title, status
    (downloaded or missed), and source (the provenance of the on-disk file:
    'deezer' or 'soulseek'). status comes from the parallel statuses list
    (same order as tracks). `sources` (optional) maps position -> provenance;
    tracks absent from it default to 'deezer'. The Soulseek sweep rewrites the
    'source' of tracks it landed via update_track_sources -- see that function.
    """
    import time as _time
    if sources is None:
        sources = {}
    if provenance is None:
        provenance = {}
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
            # Structured source of truth: keep the artist LIST (single field of
            # truth). Downstream consumers use `artists`; the old flattened
            # `artist` string was dropped to avoid a second out-of-sync copy.
            "artists": list(t.artists),
            "title": t.name,
            "album": t.album,
            "status": st,
            "source": sources.get(t.position, "deezer"),
            # Downloader's own view of this track (Deezer match / Soulseek tag),
            # recorded for reference/provenance. Absent until a file lands.
            "provenance": provenance.get(t.position),
        })
    path = out_dir / "playlist.meta.json"
    path.write_text(
        __import__("json").dumps(meta, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def update_track_sources(out_dir: Path, sources: dict[int, str], provenance: dict[int, dict] | None = None) -> None:
    """Rewrite the `source` provenance field of the named positions in
    playlist.meta.json, and flip their `status` to "downloaded" (a landed
    Soulseek file is, by definition, present -- leaving it "missed" while
    source=soulseek is internally contradictory and makes the sync gate think
    the track is absent).

    Called by the Soulseek sweep after it lands files, so the Deezer skip-check
    can tell Soulseek-sourced files from Deezer-sourced ones. Deezer ALWAYS
    wins on collision: a Soulseek-sourced file is re-fetched and overwritten on
    a later Deezer pass, which then flips that position's source back to
    'deezer' via this same function (and status stays "downloaded").
    """
    import json as _json
    path = out_dir / "playlist.meta.json"
    if not path.is_file():
        return
    try:
        data = _json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return
    for tr in data.get("tracks", []):
        pos = tr.get("position")
        if pos in sources:
            tr["source"] = sources[pos]
            tr["status"] = "downloaded"
            if provenance and pos in provenance:
                tr["provenance"] = provenance[pos]
    path.write_text(_json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
