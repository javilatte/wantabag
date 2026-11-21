"""Search public sources (YouTube Music / YouTube via yt-dlp, or SearXNG) for a track."""

from __future__ import annotations

import re
import sys
from collections.abc import Callable
from typing import Any, Literal
from urllib.parse import quote, urlparse

import requests

from wantabag.download import _default_factory, base_opts
from wantabag.matching import AUDIO, VIDEO, rank
from wantabag.models import Candidate, Track

Kind = Literal["audio", "video"]
EARLY_EXIT_SCORE = 0.85
_YT_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}


def _score_best(track: Track, cands: list[Candidate], kind: Kind) -> float:
    ranked = rank(track, cands, AUDIO if kind == "audio" else VIDEO)
    return ranked[0][0] if ranked else 0.0


def entry_to_candidate(entry: dict, origin: str) -> Candidate | None:
    """Convert a yt-dlp flat entry to a Candidate."""
    vid = entry.get("id")
    if not vid or not entry.get("title"):
        return None
    thumbs = entry.get("thumbnails") or []
    channel = entry.get("channel") or entry.get("uploader")
    if not channel and entry.get("artists"):
        channel = ", ".join(entry["artists"])
    duration = entry.get("duration")
    return Candidate(
        video_id=vid,
        url=f"https://www.youtube.com/watch?v={vid}",
        title=entry["title"],
        channel=channel,
        duration_s=int(duration) if duration else None,
        origin=origin,  # type: ignore[arg-type]
        thumbnail=thumbs[-1].get("url") if thumbs else entry.get("thumbnail"),
        album=entry.get("album"),
    )


class YtDlpSearcher:
    def __init__(
        self, verbose: bool = False, ydl_factory: Callable[[dict], Any] = _default_factory
    ) -> None:
        self.verbose = verbose
        self.opts = {**base_opts(verbose), "extract_flat": True, "playlistend": 5}
        self.factory = ydl_factory

    def _query(self, query: str, origin: str) -> list[Candidate]:
        try:
            with self.factory(self.opts) as ydl:
                info = ydl.extract_info(query, download=False)
        except Exception as e:  # noqa: BLE001
            if self.verbose:
                print(f"Warning: search failed for {query!r}: {e}", file=sys.stderr)
            return []
        entries = (info or {}).get("entries") or []
        return [c for e in entries if e and (c := entry_to_candidate(e, origin))]

    def search(self, track: Track, kind: Kind = "audio") -> list[Candidate]:
        text = f"{track.artist_display} {track.title}"
        if kind == "video":
            queries = [
                (f"ytsearch5:{track.artist_display} - {track.title} official video", "youtube")
            ]
        else:
            queries = []
            if track.isrc:
                queries.append(
                    (f"https://music.youtube.com/search?q={track.isrc}#songs", "ytmusic_song")
                )
            queries += [
                (f"https://music.youtube.com/search?q={quote(text)}#songs", "ytmusic_song"),
                (f"ytsearch5:{track.artist_display} - {track.title}", "youtube"),
            ]
        found: list[Candidate] = []
        for query, origin in queries:
            found += self._query(query, origin)
            if _score_best(track, found, kind) >= EARLY_EXIT_SCORE:
                break
        return found


def parse_length(value: Any) -> int | None:
    """Parse SearXNG 'length': seconds, 'MM:SS' or 'H:MM:SS'."""
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str) and re.fullmatch(r"\d+(:\d{1,2}){1,2}", value.strip()):
        secs = 0
        for part in value.strip().split(":"):
            secs = secs * 60 + int(part)
        return secs
    return None


class SearxngSearcher:
    def __init__(self, base_url: str, session: requests.Session, fallback: YtDlpSearcher) -> None:
        self.base_url = base_url.rstrip("/")
        self.session = session
        self.fallback = fallback
        self._warned = False

    def search(self, track: Track, kind: Kind = "audio") -> list[Candidate]:
        suffix = " official video" if kind == "video" else " audio"
        q = f"{track.artist_display} - {track.title}{suffix}"
        try:
            resp = self.session.get(
                f"{self.base_url}/search",
                params={"q": q, "format": "json", "engines": "youtube"},
            )
            resp.raise_for_status()
            results = resp.json().get("results", [])
        except (requests.RequestException, ValueError) as e:
            if not self._warned:
                print(f"Warning: SearXNG unavailable ({e}); using yt-dlp search", file=sys.stderr)
                self._warned = True
            return self.fallback.search(track, kind)
        cands = [c for r in results if (c := self._to_candidate(r))]
        return cands or self.fallback.search(track, kind)

    @staticmethod
    def _to_candidate(result: dict) -> Candidate | None:
        url = result.get("url", "")
        parsed = urlparse(url)
        if parsed.hostname not in _YT_HOSTS:
            return None
        if parsed.hostname == "youtu.be":
            vid = parsed.path.lstrip("/")
        else:
            m = re.search(r"(?:^|&)v=([\w-]{6,})", parsed.query)
            vid = m.group(1) if m else ""
        if not vid:
            return None
        return Candidate(
            video_id=vid,
            url=f"https://www.youtube.com/watch?v={vid}",
            title=result.get("title", ""),
            channel=result.get("author"),
            duration_s=parse_length(result.get("length")),
            origin="searxng",
            thumbnail=result.get("thumbnail"),
        )


def make_searcher(backend: str, searxng_url: str, session: requests.Session, verbose: bool):
    ytdlp = YtDlpSearcher(verbose)
    if backend == "searxng" and searxng_url:
        return SearxngSearcher(searxng_url, session, ytdlp)
    return ytdlp
