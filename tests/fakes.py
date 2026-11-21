"""Test doubles: a URL->response session, a searcher and a downloader."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import requests
from requests.utils import get_encoding_from_headers

from wantabag.download import DownloadError
from wantabag.models import Candidate

FIXTURES = Path(__file__).parent / "fixtures"


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class FakeResponse:
    def __init__(self, body: str | bytes = "", status: int = 200, headers: dict | None = None):
        self.content = body.encode() if isinstance(body, str) else body
        self.status_code = status
        self.headers = headers or {}
        # Like requests: text/* without a charset is decoded as ISO-8859-1, not UTF-8.
        self.encoding = get_encoding_from_headers(self.headers) or "utf-8"
        self.text = self.content.decode(self.encoding, errors="replace")

    @property
    def ok(self) -> bool:
        return self.status_code < 400

    def json(self):
        return json.loads(self.text)

    def raise_for_status(self):
        if not self.ok:
            raise requests.HTTPError(f"{self.status_code}", response=self)  # type: ignore[arg-type]


class FakeSession:
    """Maps exact URLs to a FakeResponse, or to a callable(kwargs) -> FakeResponse."""

    def __init__(self, routes: dict):
        self.routes = routes
        self.calls: list[tuple[str, dict]] = []

    def _respond(self, method: str, url: str, **kwargs):
        self.calls.append((url, kwargs))
        route = self.routes.get(url)
        if route is None:
            raise requests.ConnectionError(f"no fake route for {method} {url}")
        return route(kwargs) if callable(route) else route

    def get(self, url, **kwargs):
        return self._respond("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self._respond("POST", url, **kwargs)

    def urls(self) -> list[str]:
        return [u for u, _ in self.calls]


class FakeSearcher:
    """Returns one perfect candidate per track unless the track title is in ``missing``."""

    def __init__(self, missing: set[str] | None = None):
        self.missing = missing or set()
        self.calls: list[tuple[str, str]] = []

    def search(self, track, kind="audio"):
        self.calls.append((track.title, kind))
        if track.title in self.missing:
            return []
        vid = f"vid{abs(hash(track.title)) % 10**8:08d}"
        return [
            Candidate(
                video_id=vid,
                url=f"https://www.youtube.com/watch?v={vid}",
                title=f"{track.artist_display} - {track.title}",
                channel=f"{track.artists[0] if track.artists else 'x'} - Topic",
                duration_s=track.duration_s,
                origin="ytmusic_song",
            )
        ]


class FakeDownloader:
    """Copies a tiny valid MP3 to the destination. Titles in ``fail`` raise once per call."""

    def __init__(self, fail: set[str] | None = None):
        self.fail = fail or set()
        self.calls: list[tuple[str, str]] = []

    def _write(self, kind, cand, track, dest: Path):
        self.calls.append((kind, cand.video_id))
        if track.title in self.fail:
            raise DownloadError("boom")
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(FIXTURES / "silence.mp3", dest)
        return {"thumbnail": None, "album": "From YouTube"}

    def audio(self, cand, track, dest):
        return self._write("audio", cand, track, dest)

    def video(self, cand, track, dest):
        return self._write("video", cand, track, dest)
