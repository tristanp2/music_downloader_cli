"""Lightweight value object for a Spotify playlist track.

Keeps the shape explicit so callers don't rely on dict keys that silently
return None when misspelled (cf. Pitfall 9 — title vs name).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Track:
    position: int
    name: str
    artists: list[str]
    spotify_uri: str | None = None
    duration_ms: int | None = None
    album: str | None = None
