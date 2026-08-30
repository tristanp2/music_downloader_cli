"""Soulseek fallback: re-query Deezer-missed tracks via the Soulseek P2P
network (through the `sockseek` CLI), land them in the downloader's expected
library layout, and verify they're real lossless files.

This module is the ONLY place that knows about sockseek. It is import-safe
(outside the hot path) and does no network work of its own -- it shells out to
the sockseek binary the user installs into `tools/`.

Design notes (see SOULSEEK_FALLBACK_PLAN.md):
  * Soulseek is a P2P FILE download -- NOT real-time. Speed/availability is
    peer-driven: a track can be fast, slow, or simply absent. That unreliability
    is exactly why the Deezer pass runs first and the Soulseek sweep is kicked
    off only for the `missed` tracks, in a SINGLE merged sockseek instance per user (one login + one listen port shared by every playlist in that drain).
  * We feed sockseek a CSV of (title, artist) built from the playlist.meta.json
    we already parsed. We do NOT use sockseek's native `--input-type spotify`
    mode -- that would require Spotify dev-app credentials for no gain, since
    we already have the tracklist.
  * Files land in sockseek's own output dir in ITS naming, then this module
    copies them into the `Artist - Title` layout derived from Spotify's
    structured artist/title (see core.library.format_canonical_name) -- NOT from
    sockseek's filename. TRACKNUMBER + ALBUM are stamped for crate ordering and
    so the sync completeness gate doesn't purge them as partials.
  * A ffprobe lossless gate rejects ~320 kbps files shared under a `.flac`
    name (fake FLAC) -- those stay `missed` for Deezer to chase later.
  * Provenance: every landed file gets its meta `source` flipped to 'soulseek'.
    Deezer ALWAYS overrides on collision (see core/library.update_track_sources).
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from .config import REPO, read_conf
from .library import (
    _core_artist_title,
    _is_complete_audio,
    _normalize,
    format_canonical_name,
    playlist_lock,
    update_track_sources,
)
from .track import Track

log = None  # set by init_log()


def init_log(logger) -> None:
    """Inject the project logger so this module's messages land in the same
    rotating file as the rest of the server (avoids a print())."""
    global log
    log = logger


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------

def _tools_dir() -> Path:
    return REPO / "tools"


def _sockseek_exe() -> Path | None:
    exe = _tools_dir() / "sockseek.exe"
    return exe if exe.is_file() else None


def _sockseek_conf() -> Path:
    return _tools_dir() / "sockseek.conf"


def soulseek_enabled() -> bool:
    """True if the feature is enabled in settings.conf AND the binary is
    present. The downloader only enqueues a Soulseek sweep when this is True,
    so a missing install is a no-op (the Deezer pass still completes)."""
    cfg = read_conf(_tools_dir().parent / "config" / "settings.conf")
    if (cfg.get("soulseek_enabled") or "").strip().lower() not in ("1", "true", "yes", "on"):
        return False
    return _sockseek_exe() is not None


# ---------------------------------------------------------------------------
# ffprobe lossless gate
# ---------------------------------------------------------------------------

_FFPROBE_WARNED = False


def _ffprobe_bitrate(path: Path) -> int | None:
    """Return the audio bit_rate of `path` via ffprobe, or None if it can't be
    determined. Real CD FLAC ~ 800-1000 kbps; a `.flac` sitting at ~320 kbps is
    a padded MP3 transcode (fake FLAC)."""
    global _FFPROBE_WARNED
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=bit_rate",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=30,
        )
    except FileNotFoundError:
        if not _FFPROBE_WARNED:
            _FFPROBE_WARNED = True
            if log:
                log.warning("[soulseek] ffprobe not on PATH -- skipping lossless verification "
                            "(fake .flac files will NOT be caught)")
        return None
    except Exception:
        return None
    out = proc.stdout.strip()
    if not out:
        return None
    try:
        return int(float(out))
    except ValueError:
        return None


def _is_real_lossless(path: Path) -> bool:
    """A `.flac` must actually carry lossless content. A `.mp3` is accepted as
    whatever it is (Soulseek sharing MP3 is expected for some tracks)."""
    if path.suffix.lower() != ".flac":
        return True
    bitrate = _ffprobe_bitrate(path)
    if bitrate is None:
        # Couldn't verify -- don't block the file, but log it.
        if log:
            log.warning("[soulseek] could not verify bitrate of %s -- accepting without gate", path.name)
        return True
    if bitrate < 500_000:
        if log:
            log.warning("[soulseek] rejecting fake FLAC %s (~%d kbps, looks like a padded MP3)",
                        path.name, bitrate // 1000)
        return False
    return True


# ---------------------------------------------------------------------------
# CSV builder (from meta, NOT Spotify)
# ---------------------------------------------------------------------------

def build_missed_csv(missed: list[dict], csv_path: Path) -> None:
    """Write a `title,artist` CSV for the given missed tracks. The `missed`
    items are {name, artists} dicts (artist is a list)."""
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["title", "artist"])
        for mt in missed:
            name = mt.get("name") or ""
            artists = mt.get("artists") or []
            writer.writerow([name, " ".join(artists)])


# ---------------------------------------------------------------------------
# staging -> layout
# ---------------------------------------------------------------------------

def _resolve_position_by_containment(norm_file_title: str, position_by_title: dict[str, int]) -> int | None:
    """Fallback position match when an exact normalized-title match fails.

    sockseek's name-format yields "Artist - Title", but the Title part can carry a
    redundant artist prefix (e.g. a track titled "Jafu - In The Back" lands as
    "Jafu - Jafu - In The Back"). Splitting on the first " - " then mismatches the
    meta title ("In The Back"). If the exact title lookup misses, fall back to
    containment: a landed file title that *contains* (or is contained by) a meta
    title maps to that position. Short titles (<4 chars) are skipped to avoid
    false matches.
    """
    if len(norm_file_title) < 4:
        return None
    for meta_title, pos in position_by_title.items():
        if len(meta_title) < 4:
            continue
        if meta_title in norm_file_title or norm_file_title in meta_title:
            return pos
    return None


def _read_soulseek_title_tag(path: Path) -> str | None:
    """Read the landed file's TITLE tag (for provenance record only)."""
    try:
        from mutagen.flac import FLAC
        from mutagen.easyid3 import EasyID3
        if path.suffix.lower() == ".flac":
            audio = FLAC(str(path))
            v = audio.get("TITLE")
        else:
            audio = EasyID3(str(path))
            v = audio.get("title")
        if v:
            return str(v[0]) if isinstance(v, list) else str(v)
    except Exception:
        return None
    return None


def _read_soulseek_artist_tag(path: Path) -> str | None:
    """Read the landed file's ARTIST tag (for provenance record only)."""
    try:
        from mutagen.flac import FLAC
        from mutagen.easyid3 import EasyID3
        if path.suffix.lower() == ".flac":
            audio = FLAC(str(path))
            v = audio.get("ARTIST")
        else:
            audio = EasyID3(str(path))
            v = audio.get("artist")
        if v:
            return str(v[0]) if isinstance(v, list) else str(v)
    except Exception:
        return None
    return None


def _tag_for_layout(path: Path, position: int, total: int, album: str | None = None) -> None:
    """Set TRACKNUMBER (crate ordering) + SOURCE (download provenance) on the
    landed file, and ALBUM only when it isn't already present.

    SOURCE is a dedicated Vorbis 'SOURCE' comment (FLAC) / TXXX frame described
    'SOURCE' (MP3) -- NOT the ALBUM tag. Previously we stamped ALBUM="Soulseek"
    as a sentinel, which clobbered the real album. Now:
      * SOURCE records where the file came from (so the completeness gate and any
        future source query can see it without hijacking ALBUM).
      * ALBUM is preserved if the upload already carried one; otherwise we fall
        back to the Spotify album (when known), and only leave it blank if both
        are absent -- the completeness gate now also accepts SOURCE as proof the
        file is fully tagged.
    """
    try:
        if path.suffix.lower() == ".mp3":
            # Use ONE raw ID3 object for every frame so a second save can't drop
            # what the first wrote. EasyID3 + raw ID3 double-write was silently
            # discarding the album frame.
            from mutagen.id3 import ID3, TXXX, TRCK, TALB
            id3 = ID3(str(path), v2_version=3)
            if position:
                id3.add(TRCK(encoding=3, text=f"{position:0{len(str(total))}d}"))
            # preserve existing album; else fall back to Spotify album
            existing_album = id3.get("TALB")
            if (not existing_album or not existing_album.text) and album:
                id3.add(TALB(encoding=3, text=album))
            # SOURCE provenance (replace any prior value)
            for old in id3.getall("TXXX"):
                if old.desc == "SOURCE":
                    id3.delall("TXXX")
                    break
            id3.add(TXXX(encoding=3, desc="SOURCE", text="Soulseek"))
            id3.save(str(path), v2_version=3)
        else:
            from mutagen.flac import FLAC
            audio = FLAC(str(path))
            if position:
                audio["TRACKNUMBER"] = f"{position:0{len(str(total))}d}"
            if not audio.get("ALBUM") and album:
                audio["ALBUM"] = album
            audio["SOURCE"] = "Soulseek"
            audio.save()
    except Exception:
        pass  # tag best-effort; the file is already in place


# Max ms a Soulseek download can go with no transfer progress before sockseek
# abandons that peer and tries the next one (its own "stale download" guard).
# Default in sockseek is 30000 -- that's a long stall on a dead peer, so we cut
# it to 5000ms: a peer that hasn't moved a byte in 5s is almost certainly stalled
# and sockseek should move on to the next candidate. Tunable here.
SOULSEEK_MAX_STALE_MS = 5000

# sockseek's per-song stdout lines look like:
#   [003] SongJob: searching: Fran Cisco - Hypercube
#   [003] SongJob: downloading: Fran Cisco - Hypercube: <peer>\..\<path>
#   [003] SongJob: succeeded: Childish Gambino - Redbone: <path>
#   [003] SongJob: failed [No search results]: Fran Cisco - Hypercube
# We parse the "Artist - Title" out of the segment after "SongJob: <type>: " and
# match its normalized title against position_by_title to find the playlist pos.
_SONGJOB_RE = re.compile(r"SongJob:\s*([^:]+):\s*(.+)")

# --- concise log formatter -------------------------------------------------
# sockseek's own stdout is noisy: per-source "download attempt failed" lines
# (one per failed peer), full absolute peer paths, ".incomplete" Output lines,
# and internal [NNN] job-id prefixes. The formatter below rewrites the lines we
# care about into the project's "[soulseek] ..." style and drops the
# pure-noise lines. Anything it can't recognize is returned unchanged (printed
# plain) so we never silently swallow an unknown message.
_FMT_SONGJOB_RE = re.compile(r"^\[[^\]]*\]\s*SongJob:\s*(\w+)(?:\s*\[([^\]]*)\])?:\s*(.+)$")
_FMT_ATTEMPT_FAIL_RE = re.compile(r"^\[warn\]\s*\[jobs\]\s*\[\d+\]\s*SongJob:\s*download attempt failed:")
_FMT_EXTRACT_RE = re.compile(r"^\[[^\]]*\]\s*ExtractJob:")
_FMT_JOBLIST_RE = re.compile(r"^\[[^\]]*\]\s*Job List:")
_FMT_OUTPUT_RE = re.compile(r"^\s*Output:")
# Peer timeout: "StaleDownloadException: Download attempt became stale after
# <ms>ms without peer transfer activity: <peer>/<path>" (or "Error: Download
# attempt became stale ..."). The <peer> is the FIRST path segment.
_FMT_STALE_RE = re.compile(
    r"(?:StaleDownloadException|Error):\s*Download attempt became stale after (\d+)ms "
    r"without peer transfer activity:\s*(.+)",
    re.IGNORECASE,
)


def _format_soulseek_line(raw: str) -> str | None:
    """Return a concise `[soulseek] ...` line for a raw sockseek stdout line,
    None to drop it (noise), or the original line if it can't be parsed.

    Concise verb mapping for the per-track SongJob lines:
      searching   -> [soulseek] search: <name>
      downloading -> [soulseek] download: <name>   (one line per peer attempt)
      succeeded   -> [soulseek] got: <name>
      failed [..] -> [soulseek] miss [..]: <name>
    Dropped (pure noise): Output: lines, ExtractJob:, Job List:, and the
    per-source "download attempt failed" wrapper (the StaleDownloadException
    below already tells us the peer timed out).
    Peer timeout:  StaleDownloadException / Error "stale after <ms>ms ..." ->
      [soulseek] timeout (<ms>ms): <peer>
    Anything unrecognized passes through plain so we never swallow an unknown.
    """
    line = raw.strip()
    if not line:
        return None
    if line.startswith("[soulseek]"):
        return line  # already ours
    if _FMT_OUTPUT_RE.match(line):
        return None
    if _FMT_EXTRACT_RE.match(line):
        return None
    if _FMT_JOBLIST_RE.match(line):
        return None
    if _FMT_ATTEMPT_FAIL_RE.match(line):
        # transient per-source failure; the StaleDownloadException timeout line
        # below already reports the peer stall, so we drop this wrapper.
        return None
    sm = _FMT_STALE_RE.search(line)
    if sm:
        ms = sm.group(1)
        # The peer is the FIRST path segment of the file path sockseek reports
        # (e.g. "m1sae1/...", "DJ-Promo/AUDIO1/...", or "peer/../rest").
        peer = sm.group(2).split("/", 1)[0].split("\\", 1)[0]
        return f"[soulseek] timeout ({ms}ms): {peer}"
    m = _FMT_SONGJOB_RE.match(line)
    if m:
        typ, reason, rest = m.group(1), m.group(2), m.group(3).strip()
        # rest is "<name>" or "<name>: <peer>\..\<path>" -- keep only the name.
        name = rest.split(": ", 1)[0] if ": " in rest else rest
        if typ == "searching":
            return f"[soulseek] search: {name}"
        if typ == "downloading":
            return f"[soulseek] download: {name}"
        if typ == "succeeded":
            return f"[soulseek] got: {name}"
        if typ == "failed":
            tag = f" [{reason}]" if reason else ""
            return f"[soulseek] miss{tag}: {name}"
        return f"[soulseek] {typ}: {name}"
    return line  # unrecognized -> print plain





def _parse_songjob_line(line: str, title_to_specs: dict[str, list[tuple[int, int]]]):
    """Return (kind, matches, ok) for a parseable sockseek song line, else None.

    title_to_specs: normalized title -> [(spec_index, pos), ...] across the
    merged drain. A title may belong to several playlists (different positions),
    so the caller fans the event out to every owning spec. Best-effort: a title
    not in the map (or a multi-artist " - " mismatch) is silently skipped -- the
    end-of-drain batch still resolves every track against landed_positions.
    """
    m = _SONGJOB_RE.search(line)
    if not m:
        return None
    typ, rest = m.group(1).strip(), m.group(2)
    # Drop a trailing ": <path>" suffix if present (succeeded/downloading lines).
    name = rest.rsplit(": ", 1)[0] if ": " in rest else rest
    # name is "Artist - Title"; take the title portion after the first " - ".
    title = name.split(" - ", 1)[1] if " - " in name else name
    matches = title_to_specs.get(_normalize(title))
    if not matches:
        return None
    # `searching` is informational -- it goes to the server log (formatted) but
    # emits no live event (the row shouldn't flip to DOWNLOADING until a peer is
    # actually being fetched). `downloading` -> start; `succeeded`/`failed` -> done.
    if typ == "downloading":
        return ("start", matches, True)
    if typ.startswith("succeeded"):
        return ("done", matches, True)
    if typ.startswith("failed"):
        return ("done", matches, False)
    return None


def _rm_sweep_dir(sweep_dir: Path) -> None:
    """Remove the per-run staging dir entirely (nested .sockseek-staging/<hash>/
    folders, .incomplete files, the CSV -- everything). Landed files were already
    moved into out_dir before this runs, so nothing real is lost. Best-effort:
    a transient lock or in-use file won't abort the sweep result."""
    try:
        if sweep_dir.exists():
            shutil.rmtree(sweep_dir, ignore_errors=True)
    except Exception:
        pass


def run_soulseek_drain(specs: list[dict], work_dir: Path) -> dict:
    """Run ONE sockseek process for all `specs` (a single merged drain).

    This replaces the old per-playlist subprocess model. Launching one sockseek
    per playlist caused N simultaneous logins that all tried to bind the same
    listen port (49998) and stomped on each other -- only one would win, the
    rest died with "Failed to start listening". A single merged instance logs in
    once, binds the port once, and downloads every playlist's misses in one
    search session (also faster). `user` is irrelevant here: it's only an
    output-path prefix handled by each spec's own out_dir, and the single
    sockseek process always uses the shared tools/sockseek.conf account. Each
    spec still gets its own job card + live events; the shared stream is fanned
    out to the owning spec(s) for every track (a track wanted by several
    playlists routes to all of them).

    Each spec dict:
        folder, user, out_dir (Path), missed (list[dict]), total (int),
        position_by_title (norm_title -> pos), on_progress(msg),
        on_event(kind, pos, ok)

    Returns {spec_index: {"ok", "downloaded", "missed", "landed_positions",
    "error"}, ...}.
    """
    results: dict[int, dict] = {}
    if not specs:
        return results

    # Build one merged CSV (deduped by name+artists) + the title->specs map.
    merged: dict[tuple, dict] = {}
    title_to_specs: dict[str, list[tuple[int, int]]] = {}
    for idx, spec in enumerate(specs):
        for mt in spec["missed"]:
            key = (mt.get("name") or "", tuple(mt.get("artists") or []))
            merged.setdefault(key, mt)
            norm = _normalize(mt.get("name") or "")
            if norm:
                title_to_specs.setdefault(norm, []).append(
                    (idx, mt.get("position") or 0))

    # Shared staging dir for this drain (under the user's work_dir).
    sweep_dir = work_dir / ".soulseek_sweep"
    if sweep_dir.exists():
        _rm_sweep_dir(sweep_dir)
    sweep_dir.mkdir(parents=True, exist_ok=True)
    csv_path = sweep_dir / "missed.csv"
    build_missed_csv(list(merged.values()), csv_path)

    # Announce each playlist in the server log + its own job log.
    for spec in specs:
        msg = f"[soulseek] sweeping {len(spec['missed'])} missed tracks for '{spec['folder']}'"
        if log:
            log.info(msg)
        spec["on_progress"](msg)

    exe = _sockseek_exe()
    if exe is None:
        return {i: {"ok": False, "error": "sockseek.exe not found in tools/",
                    "downloaded": 0, "missed": len(s["missed"]),
                    "landed_positions": []} for i, s in enumerate(specs)}
    conf = _sockseek_conf()
    if not conf.is_file():
        return {i: {"ok": False, "error": "sockseek.conf not found in tools/",
                    "downloaded": 0, "missed": len(s["missed"]),
                    "landed_positions": []} for i, s in enumerate(specs)}

    cmd = [
        str(exe),
        "--config", str(conf),
        "--input-type", "csv",
        "--pref-format", "flac,wav",
        # Bare "Artist - Title" filename (no artist/album subfolders) so the
        # downloader's existing Artist - Title parser + TRACKNUMBER logic
        # line up. Falls back to the original filename if untagged.
        "--name-format", "{artist( - )title|filename}",
        # Abandon a stalled peer faster than sockseek's 30s default (see
        # SOULSEEK_MAX_STALE_MS). A dead peer gets dropped and sockseek retries
        # the next candidate instead of hanging the whole sweep.
        "--max-stale-time", str(SOULSEEK_MAX_STALE_MS),
        # Point sockseek's output at this drain's staging dir.
        "--output-dir", str(sweep_dir),
        str(csv_path),
    ]

    proc = None
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
        assert proc.stdout is not None
        for raw in proc.stdout:
            raw = raw.rstrip("\n")
            if not raw.strip():
                continue
            fmt = _format_soulseek_line(raw)
            if fmt is None:
                continue
            parsed = _parse_songjob_line(raw, title_to_specs)
            matched = parsed[1] if parsed else []
            # Server log gets the concise line once; each owning job gets it too.
            if log:
                log.info(fmt)
            for (sidx, _) in matched:
                specs[sidx]["on_progress"](fmt)
            if parsed is not None:
                kind, matches, ok = parsed
                for (sidx, pos) in matches:
                    specs[sidx]["on_event"](kind, pos, ok)
        proc.wait(timeout=3600)
    except subprocess.TimeoutExpired:
        if proc is not None:
            try:
                proc.kill()
            except Exception:
                pass
        if log:
            log.warning("[soulseek] drain timed out (1h) -- partial results kept")
        proc = None
    except Exception as e:
        if log:
            log.error("[soulseek] drain failed: %s", e)
        proc = None

    # Stage: copy each landed file into every playlist that wanted it, tagged
    # with THAT playlist's TRACKNUMBER. A single physical download can satisfy
    # several playlists (different positions -> different copies).
    try:
        landed_files = [
            f for f in (*sweep_dir.rglob("*.flac"), *sweep_dir.rglob("*.mp3"))
            if _is_complete_audio(f) and _is_real_lossless(f)
        ]
    except Exception:
        landed_files = []
    for idx, spec in enumerate(specs):
        out_dir = spec["out_dir"]
        out_dir.mkdir(parents=True, exist_ok=True)
        pbt = spec["position_by_title"]
        meta_by_pos = spec.get("meta_by_pos", {})
        total = spec["total"]
        landed_positions: set[int] = set()
        provenance: dict[int, dict] = {}
        for f in landed_files:
            _, file_title = _core_artist_title(f.stem)
            norm = _normalize(file_title)
            pos = pbt.get(norm)
            if pos is None:
                pos = _resolve_position_by_containment(norm, pbt)
            if pos is None:
                continue
            # Name from Spotify's data (the source of truth), NOT sockseek's
            # output filename. Falls back to the landed stem if meta is missing.
            meta_artists, meta_title, meta_album = meta_by_pos.get(pos, ([], "", ""))
            canonical = format_canonical_name(meta_artists, meta_title)
            core = canonical or f.stem
            target = out_dir / f"{core}{f.suffix}"
            try:
                shutil.copy2(f, target)
            except Exception as e:
                if log:
                    log.warning("[soulseek] could not copy %s -> %s: %s", f.name, target, e)
                continue
            _tag_for_layout(target, pos, total, meta_album)
            landed_positions.add(pos)
            # Record the downloader's own view (the landed file's real tag) for
            # reference/provenance, alongside Spotify's truth in the meta.
            tag_title = _read_soulseek_title_tag(f)
            tag_artist = _read_soulseek_artist_tag(f)
            provenance[pos] = {
                "source": "soulseek",
                "original_filename": f.name,
                "tag_title": tag_title,
                "tag_artist": tag_artist,
                "filename": target.name,
            }
        if landed_positions:
            update_track_sources(
                out_dir,
                {p: "soulseek" for p in landed_positions},
                provenance=provenance,
            )
        results[idx] = {
            "ok": True,
            "downloaded": len(landed_positions),
            "missed": len(spec["missed"]) - len(landed_positions),
            "landed_positions": sorted(landed_positions),
        }
    # Always wipe the staging dir so a stale nested .sockseek-staging/<hash>/ or
    # leftover .incomplete can't poison the next rerun.
    _rm_sweep_dir(sweep_dir)
    return results