"""Enumerations for SSE event types and download/status strings.

Two ``str``-based enums (so members serialize to their string value in
JSON for the SSE wire format, unchanged from the plain-dict days):

* ``JobEventType`` -- the ``type`` field on every SSE event pushed to
  ``/jobs/{id}/events`` and read by ``app.py:_run_job.on_event``.
* ``DownloadStatus`` -- the ``status`` field on a ``done`` event and on a
  ``TrackState``, plus the terminal ``Job.status`` values ``done``/``error``.

Using these instead of bare string literals means a typo (``"track"`` vs
``"tracks"``) is a ``NameError`` at import time, not a silently-dropped
event. The producer (``core/downloader.py``) and the single consumer
(``app.py``'s ``on_event`` switch) both reference the members.
"""
from __future__ import annotations

from enum import Enum


class JobEventType(str, Enum):
    TRACKS = "tracks"
    START = "start"
    PCT = "pct"
    DONE = "done"
    JOB_DONE = "job_done"
    JOB_CREATED = "job_created"   # shared job-feed broadcast (any job, any user)


class DownloadStatus(str, Enum):
    # per-track end states (TrackState.status + done-event status)
    DOWNLOADED = "downloaded"
    SKIPPED = "skipped"
    MISSED = "missed"
    FAILED = "failed"
    # transient / live states (TrackState.status only)
    PENDING = "pending"
    DOWNLOADING = "downloading"
    # terminal Job.status values
    OK = "done"
    ERROR = "error"
