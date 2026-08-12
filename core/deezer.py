"""Deezer session init, search (with Jaccard title scoring), and FLAC download
via deemix. The ProgressListener bridges deemix to a rich progress bar.
"""
import contextlib
import logging
import re
from pathlib import Path

from deezer import Deezer, TrackFormats
from deemix import generateDownloadObject
from deemix.settings import load as loadSettings
from deemix.downloader import Downloader
from rich.progress import Progress, BarColumn, TextColumn, TaskProgressColumn

from .config import REPO

log = logging.getLogger("musicdl")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _dz_field(track, *keys, default=""):
    """Read a nested key from a Deezer result dict, tolerating missing fields."""
    cur = track
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
    return cur if isinstance(cur, str) else default


def _tokenize(s):
    """Lowercase, strip punctuation, split on whitespace -> set of tokens."""
    return set(re.sub(r'[^a-z0-9]+', ' ', (s or '').lower()).split())


def _title_similarity(a, b):
    """Jaccard similarity between two title strings (word tokens)."""
    ta, tb = _tokenize(a), _tokenize(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------

def deezer_search(dz, query, target_title=None, target_artists=None):
    """Return the best Deezer track dict for a query, or None.

    Deezer's search ranks by popularity, so the top hit is often NOT the track
    we asked for when several similarly-named releases exist (e.g. 'Hedonic
    Setpoint 85' vs '... 87' vs '... 70'). We score ALL returned results on
    title+artist similarity to the target and pick the best match. Results
    below 0.5 title similarity are rejected so a totally wrong popular track
    is never downloaded.
    """
    try:
        res = dz.api.search(query)
    except Exception:
        return None
    data = res.get("data") or []
    if not data:
        return None

    want_title = target_title or ""
    want_artist = " ".join(target_artists or [])

    def score(track):
        t_title = _dz_field(track, "title")
        t_artist = _dz_field(track, "artist", "name")
        # Use Jaccard as a gradient (not just pass/fail) so 'Hedonic Setpoint
        # 85' (1.0) beats '... 70' / '... 87' (0.50 each) when Deezer ranks
        # the wrong one first. Artist match is a bonus, not required.
        title_sim = _title_similarity(want_title, t_title)
        if title_sim < 0.5:
            return 0
        artist_sim = _title_similarity(want_artist, t_artist) if want_artist and t_artist else 0
        # title contributes 0-1, artist contributes up to 0.5
        return title_sim + (0.5 if artist_sim >= 0.5 else 0)

    best = None
    best_score = 0
    for track in data:
        s = score(track)
        if s > best_score:
            best_score = s
            best = track
    # require at least a partial title match
    return best if best_score >= 0.5 else None


# ---------------------------------------------------------------------------
# download
# ---------------------------------------------------------------------------

class ProgressListener:
    """Bridge deemix's listener interface to progress reporting.

    Two modes:
    - CLI (on_progress is None): render a live rich progress bar via the
      `progress`/`task_id` handles.
    - server/cron (on_progress is set): emit plain text lines in ~BUCKET_SIZE%
      increments. The rich TUI is suppressed because its ANSI control codes
      mangle the server log / interleave with uvicorn access logs.

    deemix calls .send(key, value); value['progress'] is 0-100 for the
    current track. In server mode we only log when the integer percent jumps
    to a new 10% bucket, so a long download doesn't flood the log.
    """

    BUCKET_SIZE = 10

    def __init__(self, progress=None, task_id=None, label="", on_progress=None):
        self.progress = progress
        self.task_id = task_id
        self.label = label
        self.on_progress = on_progress
        self._last_bucket = -1

    def send(self, key, value=None):
        if key == "updateQueue" and isinstance(value, dict):
            pct = value.get("progress")
            if isinstance(pct, (int, float)):
                pct_int = int(pct)
                bucket = pct_int // self.BUCKET_SIZE
                # Throttle both SSE and text logging to 10% buckets.
                if bucket == self._last_bucket:
                    return
                self._last_bucket = bucket
                rounded_pct = bucket * self.BUCKET_SIZE
                # structured event callback (server mode)
                if getattr(self, '_on_pct', None):
                    self._on_pct(rounded_pct)
                if self.on_progress is not None:
                    msg = f"    {self.label} [{rounded_pct}%]"
                    self.on_progress(msg)
                if self.progress is not None:
                    self.progress.update(self.task_id, completed=int(pct))
                return
        if key == "downloadInfo" and isinstance(value, dict):
            state = value.get("state")
            if state in ("downloading", "getBitrate", "getTags", "getAlbumArt",
                         "tagging", "downloaded", "alreadyDownloaded"):
                if self.on_progress is not None:
                    msg = f"    {self.label} [{state}]"
                    log.info(msg)
                    self.on_progress(msg)
                elif self.progress is not None:
                    self.progress.update(self.task_id, description=f"{self.label} [{state}]")
            return
        # fall back to deemix's own human-readable line for anything else
        from deemix.utils import formatListener
        line = formatListener(key, value)
        if line:
            if self.on_progress is not None:
                self.on_progress(f"    {line}")
                log.info("    %s", line)
            elif self.progress is not None:
                self.progress.console.print(f"    {line}")


def deemix_download(dz, deezer_url, settings, out_dir, label, on_progress=None, on_pct=None):
    """Download a Deezer URL as FLAC via the deemix library (not subprocess).

    Forces a FLAT layout (no artist/album subfolders) so the file lands directly
    in out_dir, which keeps playlist ordering sane for players like the Denon
    Prime 4. Returns the downloaded FLAC path, or None on failure.

    on_progress: when set (server/cron), the live rich progress bar is
    suppressed and ProgressListener logs plain increment text lines
    through on_progress instead, so server logs stay clean (no ANSI garbage).
    on_pct: when set, fires on_pct(pct_int) for every deemix updateQueue event.

    Robustness: we snapshot the .flac filenames present BEFORE the download and
    then identify the genuinely NEW file afterwards. Relying on "newest mtime"
    alone is fragile when out_dir already contains other .flac files -- the
    pre-existing files can have a newer mtime than the just-finalized download,
    causing us to return the wrong path and leave the real download unrenamed
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    # set the output location + flat layout at runtime (override config.json)
    settings["downloadLocation"] = str(out_dir)
    for key in ("createArtistFolder", "createAlbumFolder", "createSingleFolder",
                "createCDFolder", "createStructurePlaylist"):
        settings[key] = False
    before = {f.name for f in out_dir.glob("*.flac")}
    baseline_mtime = max((f.stat().st_mtime for f in out_dir.glob("*.flac")),
                         default=0.0)
    try:
        download_object = generateDownloadObject(dz, deezer_url, TrackFormats.FLAC,
                                                  None, None)
    except Exception as e:
        msg = f"    [deezer] generate failed: {e}"
        log.warning(msg)
        if on_progress:
            on_progress(msg)
        return None
    listener = ProgressListener(on_progress=on_progress, label=label)
    if on_progress is None:
        # CLI mode: live rich bar
        with Progress(
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            transient=True,
        ) as progress:
            task_id = progress.add_task(label, total=100)
            listener.progress = progress
            listener.task_id = task_id
            try:
                Downloader(dz, download_object, settings, listener=listener).start()
            except Exception as e:
                msg = f"    [deezer] download failed: {e}"
                log.warning(msg)
                if on_progress:
                    on_progress(msg)
                return None
    else:
        # server/cron mode: no TUI, wire on_pct for structured events
        listener._on_pct = on_pct
        try:
            Downloader(dz, download_object, settings, listener=listener).start()
        except Exception as e:
            log.warning("    [deezer] download failed: %s", e)
            return None
    # Identify the file we just created: it must be NEWER than the baseline
    # captured before the download started. A genuinely failed download (e.g.
    # "Track not found at desired bitrate") writes nothing, so no file will
    # exceed the baseline -- in that case we return None and the caller logs it
    # as missed, rather than returning a stale .flac already in the folder.
    candidates = [f for f in out_dir.glob("*.flac")
                  if f.stat().st_mtime > baseline_mtime + 0.01]
    if not candidates:
        return None
    # Prefer a file whose name did not exist before; otherwise the newest one.
    for f in sorted(candidates, key=lambda p: p.stat().st_mtime, reverse=True):
        if f.name not in before:
            return f
    return candidates[0]


# ---------------------------------------------------------------------------
# session init
# ---------------------------------------------------------------------------

def init_deezer(arl_text):
    """Create + log in a Deezer session via ARL. Returns the session or dies."""
    dz = Deezer()
    if not dz.login_via_arl(arl_text):
        raise RuntimeError("Deezer ARL login failed (token in deezer.arl may be expired).")
    return dz
