"""Normalized domain types shared by every playlist source."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from wantabag.matching import norm


@dataclass(frozen=True)
class Track:
    title: str
    artists: tuple[str, ...]
    album: str | None = None
    duration_ms: int | None = None
    isrc: str | None = None
    artwork_url: str | None = None
    source_id: str = ""
    artist_display: str = ""

    def __post_init__(self) -> None:
        if not self.artist_display:
            object.__setattr__(self, "artist_display", ", ".join(self.artists))

    @property
    def duration_s(self) -> int | None:
        return None if self.duration_ms is None else round(self.duration_ms / 1000)

    def key(self) -> str:
        """Stable identity used by the local state and to detect repeats."""
        if self.isrc:
            return f"isrc:{self.isrc.upper()}"
        return f"t:{norm(self.artist_display)}|{norm(self.title)}"

    @property
    def label(self) -> str:
        return f"{self.artist_display} - {self.title}"


@dataclass(frozen=True)
class Playlist:
    source: Literal["apple", "spotify"]
    id: str
    url: str
    name: str
    tracks: tuple[Track, ...]
    declared_count: int | None = None


@dataclass(frozen=True)
class Candidate:
    video_id: str
    url: str
    title: str
    channel: str | None
    duration_s: int | None
    origin: Literal["ytmusic_song", "youtube", "searxng"]
    thumbnail: str | None = None
    album: str | None = None
