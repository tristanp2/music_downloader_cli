"""Server-side job state for the web UI.

A Job is created the moment a download is requested and carries its
progress, live log, and final result. It is initialised fully up front --
every field has a defined value at construction (no phased mutation where a
background thread fills keys in later), so callers never hit a missing key
because the worker thread hasn't populated it yet.

TrackState is the per-track progress row the frontend renders; it mirrors
the shape of the SSE ``tracks``/``done`` events so the /jobs/{id} poll can
hand the exact same structure back after an SSE reconnect.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from core.downloader import DispatchResult
from typing import Optional


@dataclass
class TrackState:
    pos: int
    name: str
    status: str = "pending"   # pending|downloading|skipped|missed|failed|downloaded
    pct: int = 0


@dataclass
class JobProgress:
    total: Optional[int] = None
    tracks: dict[int, TrackState] = field(default_factory=dict)


@dataclass
class DownloadJob:
    id: str
    url: str
    user: str
    playlist_name: str = ""                              
    status: str = "queued"                  # queued|running|done|error
    log: list[str] = field(default_factory=list)
    result: DispatchResult = field(default_factory=DispatchResult) 
    started_at: str = ""
    finished_at: Optional[str] = None
    progress: JobProgress = field(default_factory=JobProgress)
