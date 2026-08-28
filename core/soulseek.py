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
    off only for the `missed` tracks, in a SEPARATE parallel worker.
  * We feed sockseek a CSV of (title, artist) built from the playlist.meta.json
    we already parsed. We do NOT use sockseek's native `--input-type spotify`
    mode -- that would require Spotify dev-app credentials for no gain, since
    we already have the tracklist.
  * Files land in sockseek's own output dir in ITS naming, then this module
    moves + re-tags them into the bare `Artist - Title.flac` layout the
    downloader/exporter expect (TRACKNUMBER for crate ordering, ALBUM so the
    sync completeness gate doesn't purge them as partials).
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

def _match_csv_row_to_file(row_title: str, row_artist: str, candidate: Path) -> bool:
    """Loose match of a landed file to its CSV row by title (artist is often
    truncated/normalised by the peer, so we match on the title core only)."""
    want_title = _normalize(row_title)
    if not want_title:
        return False
    _, file_title = _core_artist_title(candidate.stem)
    return want_title == _normalize(file_title)


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


def _stage_into_layout(source_dir: Path, out_dir: Path, position_by_title: dict[str, int],
                       total: int) -> list[tuple[int, Path]]:
    """Move + re-tag every COMPLETE audio file from sockseek's output dir into
    the downloader's bare `Artist - Title.flac` layout, setting TRACKNUMBER
    (from the meta position) and ALBUM (so the sync completeness gate treats it
    as a finished file, not a partial -- see P35).

    Returns [(position, final_path), ...] for the files that landed and passed
    the lossless gate. Files that fail the gate are left in source_dir (so a
    re-run can retry) and NOT reported as landed.
    """
    landed: list[tuple[int, Path]] = []
    # sockseek nests files under <output-dir>/<jobname>/<artist>/... so we must
    # walk recursively -- a flat glob would miss everything (the original bug:
    # the sweep reported 0 landed because the file sat one folder deeper).
    for audio_file in (*source_dir.rglob("*.flac"), *source_dir.rglob("*.mp3")):
        if not _is_complete_audio(audio_file):
            continue
        if not _is_real_lossless(audio_file):
            # Fake FLAC (padded MP3): leave in place, don't import. Stays missed.
            continue
        _, file_title = _core_artist_title(audio_file.stem)
        norm_title = _normalize(file_title)
        position = position_by_title.get(norm_title)
        if position is None:
            # Exact title match failed (e.g. a redundant artist prefix in the
            # landed filename like "Jafu - Jafu - In The Back" vs meta "In The
            # Back"). Fall back to containment so the file still maps to its real
            # playlist position instead of being dropped to 0 (mis-tagged 01).
            position = _resolve_position_by_containment(norm_title, position_by_title)
        if position is None:
            # Still no mapping -> put it in the folder with no TRACKNUMBER.
            # Better partially present than missing.
            position = 0
        artist_part, _ = _core_artist_title(audio_file.stem)
        target_name = f"{audio_file.stem}{audio_file.suffix}"
        target = out_dir / target_name
        try:
            # os.replace overwrites atomically on every platform (Path.rename
            # raises FileExistsError on Windows when the target already exists).
            # A re-run of the sweep legitimately replaces a prior Soulseek file.
            os.replace(audio_file, target)
        except Exception as e:
            if log:
                log.warning("[soulseek] could not move %s -> %s: %s", audio_file.name, target, e)
            continue
        _tag_for_layout(target, position, total)
        if position:
            landed.append((position, target))
    return landed


def _tag_for_layout(path: Path, position: int, total: int) -> None:
    """Set TRACKNUMBER (crate ordering) + ALBUM (completeness) on the landed
    file. ALBUM is required so the sync completeness gate (_is_complete_audio)
    doesn't treat this as a partial and purge it. We can't know the real album
    from Soulseek, so we stamp a stable sentinel; the exporter only needs a
    non-empty ALBUM to render the crate."""
    try:
        if path.suffix.lower() == ".mp3":
            from mutagen.easyid3 import EasyID3
            audio = EasyID3(str(path))
            if position:
                audio["tracknumber"] = f"{position:0{len(str(total))}d}"
            audio["album"] = "Soulseek"
            audio.save()
        else:
            from mutagen.flac import FLAC
            audio = FLAC(str(path))
            if position:
                audio["TRACKNUMBER"] = f"{position:0{len(str(total))}d}"
            audio["ALBUM"] = "Soulseek"
            audio.save()
    except Exception:
        pass  # tag best-effort; the file is already in place


# ---------------------------------------------------------------------------
# sweep
# ---------------------------------------------------------------------------

# Max ms a Soulseek download can go with no transfer progress before sockseek
# abandons that peer and tries the next one (its own "stale download" guard).
# Default in sockseek is 30000 -- that's a long stall on a dead peer, so we cut
# it to 10s: a peer that hasn't moved a byte in 10s is almost certainly stalled
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
_TYPE_START = {"searching", "downloading"}


def _parse_songjob_line(line: str, position_by_title: dict[str, int]):
    """Return ("start"|"done", pos) for a parseable sockseek song line, else None.

    Best-effort: matched on the normalized track title, so a title that differs
    from the Deezer name (or a multi-artist " - " in the title) may not match and
    is silently skipped -- the end-of-sweep batch still resolves every track.
    """
    m = _SONGJOB_RE.search(line)
    if not m:
        return None
    typ, rest = m.group(1).strip(), m.group(2)
    # Drop a trailing ": <path>" suffix if present (succeeded/downloading lines).
    name = rest.rsplit(": ", 1)[0] if ": " in rest else rest
    # name is "Artist - Title"; take the title portion after the first " - ".
    title = name.split(" - ", 1)[1] if " - " in name else name
    pos = position_by_title.get(_normalize(title))
    if pos is None:
        return None
    if typ in _TYPE_START:
        return ("start", pos, True)
    if typ.startswith("succeeded"):
        return ("done", pos, True)
    if typ.startswith("failed"):
        return ("done", pos, False)
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


def run_soulseek_sweep(folder: str, user: str, work_dir: Path, missed: list[dict],
                       total: int, on_progress=None, on_event=None) -> dict:
    """Run a Soulseek sweep for one playlist's Deezer-missed tracks.

    Parameters
    ----------
    folder : str
        The safe playlist folder name (same as the Deezer pass used).
    user : str
        Owning user (drives WORK_DIR_BASE/<user>/<folder>).
    work_dir : Path
        The Deezer pass's work_dir (WORK_DIR_BASE/<user>) -- the playlist folder
        is work_dir / folder.
    missed : list[dict]
        [{name, artists}, ...] for the Deezer-missed tracks.
    total : int
        Track count of the playlist (for TRACKNUMBER zero-padding).
    on_progress : callable(msg) | None
        Optional text hook (wired to the job log + server log in app.py). Each
        sockseek stdout line is reported here, so the full trace lands in both.
    on_event : callable(kind, pos, ok) | None
        Optional live-progress hook. For each sockseek line we can parse, calls
        on_event("start", pos, True) on searching/downloading and
        on_event("done", pos, ok) on succeeded (ok=True) / failed (ok=False) --
        so the frontend can reflect Soulseek progress in the job card as it
        happens (best-effort; title-matched, not byte-precise).

    Returns a result dict: {ok, downloaded, missed, error, landed_positions, sweep_dir}.
    """
    def report(msg):
        if log:
            log.info(msg)
        if on_progress:
            on_progress(msg)
        else:
            print(msg)

    out_dir = work_dir / folder
    out_dir.mkdir(parents=True, exist_ok=True)
    # Serialize cross-source disk work on THIS playlist with the Deezer drain.
    with playlist_lock(out_dir):
        exe = _sockseek_exe()
        if exe is None:
            return {"ok": False, "error": "sockseek.exe not found in tools/",
                    "downloaded": 0, "missed": len(missed)}
        conf = _sockseek_conf()
        if not conf.is_file():
            return {"ok": False, "error": "sockseek.conf not found in tools/",
                    "downloaded": 0, "missed": len(missed)}

        # Build the position-by-title map for staging (title normalized -> pos).
        position_by_title = {}
        for mt in missed:
            norm = _normalize(mt.get("name") or "")
            if norm:
                position_by_title[norm] = mt.get("position") or 0

        # sockseek writes to ITS OWN output dir (from sockseek.conf). We stage
        # into a per-run subfolder so we don't accidentally re-import files from
        # a previous run, then move only what this run produced.
        sweep_dir = out_dir / ".soulseek_sweep"
        # Start clean: a stale sweep dir from a previous run (nested
        # .sockseek-staging/<hash>/ folders + .incomplete files) survived the old
        # cleanup and caused issues on reruns. Always wipe whatever is there first.
        if sweep_dir.exists():
            _rm_sweep_dir(sweep_dir)
        sweep_dir.mkdir(parents=True, exist_ok=True)
        csv_path = sweep_dir / "missed.csv"
        build_missed_csv(missed, csv_path)

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
            # SOULSEEK_MAX_STALE_MS). A dead peer gets dropped at 10s and sockseek
            # retries the next candidate instead of hanging the whole sweep.
            "--max-stale-time", str(SOULSEEK_MAX_STALE_MS),
            # Point sockseek's output at this run's staging dir. The long flag
            # is --output-dir (NOT --output -- that's an unknown arg and the
            # sweep exits 2). _stage_into_layout then moves landed files out of
            # sweep_dir into the playlist folder.
            "--output-dir", str(sweep_dir),
            str(csv_path),
        ]
        report(f"[soulseek] sweeping {len(missed)} missed tracks for '{folder}'")
        # Stream sockseek's output line-by-line into the job log so the per-track
        # search/succeed/fail activity is visible (previously discarded on success,
        # because subprocess.run(capture_output=True) threw stdout away). Merge
        # stderr into stdout; report each non-blank line as it arrives.
        proc = None
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1,
            )
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.rstrip("\n")
                if not line.strip():
                    continue
                report(line)  # -> job log + server log (report() calls log.info)
                if on_event:
                    parsed = _parse_songjob_line(line, position_by_title)
                    if parsed is not None:
                        kind, pos, ok = parsed
                        on_event(kind, pos, ok)
            proc.wait(timeout=3600)
        except subprocess.TimeoutExpired:
            if proc is not None:
                try:
                    proc.kill()
                except Exception:
                    pass
            report("[soulseek] sweep timed out (1h) -- partial results kept")
            proc = None
        except Exception as e:
            result = {"ok": False, "error": f"sockseek failed: {e}",
                      "downloaded": 0, "missed": len(missed),
                      "landed_positions": [], "sweep_dir": str(sweep_dir)}
        else:
            if proc is not None and proc.returncode != 0:
                report(f"[soulseek] sockseek exited {proc.returncode}")

            landed = _stage_into_layout(sweep_dir, out_dir, position_by_title, total)
            landed_positions = {pos for pos, _ in landed}
            # Flip provenance for landed tracks so Deezer can override later.
            if landed_positions:
                update_track_sources(out_dir, {pos: "soulseek" for pos in landed_positions})
            result = {
                "ok": True,
                "downloaded": len(landed),
                "missed": len(missed) - len(landed),
                "landed_positions": sorted(landed_positions),
                "sweep_dir": str(sweep_dir),
            }
        finally:
            # Always remove the per-run staging dir so a stale nested
            # .sockseek-staging/<hash>/ or leftover .incomplete can't poison the
            # next rerun. Landed files were already moved into out_dir.
            _rm_sweep_dir(sweep_dir)
        return result
