"""Web jobs: a small queue of playlist downloads, each in its own temporary folder."""

from __future__ import annotations

import secrets
import shutil
import threading
import time
import traceback
import zipfile
from collections import deque
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import requests

from wantabag import searxng
from wantabag.config import (
    AUDIO_QUALITIES,
    RECOMMENDED_VIDEO,
    VIDEO_QUALITIES,
    Config,
    ConfigError,
    load_config,
)
from wantabag.download import Downloader
from wantabag.files import playlist_dir, sanitize_filename
from wantabag.http import make_session
from wantabag.models import Playlist
from wantabag.search import make_searcher
from wantabag.sources import UNSUPPORTED, parse_playlist_url, read_playlist
from wantabag.sources.apple import AppleError
from wantabag.sources.spotify import SpotifyError
from wantabag.state import State
from wantabag.sync import sync_playlist

JOB_ERRORS = (
    ConfigError,
    ValueError,
    AppleError,
    SpotifyError,
    searxng.SearxngError,
    requests.RequestException,
)
MAX_URL = 2048
MAX_NAME = 200
JobStatus = Literal["queued", "reading", "downloading", "done", "error"]


class QueueFull(Exception):
    pass


def validate_url(url: object) -> str:
    """Only https links to Apple Music and Spotify playlists (no ``spotify:`` URIs)."""
    if not isinstance(url, str) or len(url) > MAX_URL:
        raise ValueError(UNSUPPORTED)
    url = url.strip()
    if urlparse(url).scheme != "https":
        raise ValueError(UNSUPPORTED)
    parse_playlist_url(url)
    return url


def apply_options(body: dict, cfg: Config) -> tuple[Config, str]:
    """Validate the optional fields of a job request; return the config to use and the name.

    ``videos`` is ``false``, ``true`` (recommended quality) or a key of ``VIDEO_QUALITIES``.
    Anything left out keeps the user's configured value.
    """
    quality = body.get("quality", cfg.audio_quality)
    if quality not in AUDIO_QUALITIES:
        raise ValueError(f"quality must be one of {list(AUDIO_QUALITIES)}")
    name = body.get("name") or ""
    if not isinstance(name, str) or len(name) > MAX_NAME:
        raise ValueError(f"name must be text of at most {MAX_NAME} characters")
    videos = body.get("videos", cfg.video_quality if cfg.video_enabled else False)
    if videos is True:
        videos = RECOMMENDED_VIDEO
    if videos in (False, None):
        enabled, video_quality = False, cfg.video_quality
    elif isinstance(videos, str) and videos in VIDEO_QUALITIES:
        enabled, video_quality = True, videos
    else:
        raise ValueError(f"videos must be false, true or one of {list(VIDEO_QUALITIES)}")
    cfg = replace(cfg, audio_quality=quality, video_enabled=enabled, video_quality=video_quality)
    return cfg, name.strip()


def summarize(playlist: Playlist) -> dict:
    """What the page needs to show a playlist (also the payload of the ``playlist`` event)."""
    tracks = [
        {
            "index": i,
            "title": t.title,
            "artist": t.artist_display,
            "label": t.label,
            "duration_s": t.duration_s,
        }
        for i, t in enumerate(playlist.tracks)
    ]
    artwork = next((t.artwork_url for t in playlist.tracks if t.artwork_url), None)
    return {
        "name": playlist.name,
        "source": playlist.source,
        "count": len(tracks),
        "declared_count": playlist.declared_count,
        "total_duration_s": sum(t["duration_s"] or 0 for t in tracks),
        "artwork": artwork if artwork and artwork.startswith("https://") else None,
        "tracks": tracks,
    }


def preview(url: str) -> dict:
    """Read a playlist without downloading anything. ``url`` must already be validated."""
    return summarize(read_playlist(url, load_config(), make_session()))


class Job:
    """One download. Every field below ``cond`` is read and written holding that lock."""

    def __init__(
        self, id: str, url: str, cfg: Config, name: str, dir: Path, clock: Callable[[], float]
    ) -> None:
        self.id, self.url, self.cfg, self.name, self.dir = id, url, cfg, name, dir
        self.clock = clock
        self.cond = threading.Condition()
        self.status: JobStatus = "queued"
        self.position = 0
        self.playlist: dict | None = None
        self.tracks: list[dict] = []
        self.events: list[dict] = []
        self.error: str | None = None
        self.finished_at: float | None = None
        self.zip_path: Path | None = None

    def _apply(self, type: str, data: dict) -> None:
        if type == "queued":
            self.status, self.position = "queued", data["position"]
        elif type == "playlist":
            self.status, self.playlist = "downloading", data
            video = "waiting" if self.cfg.video_enabled else None
            self.tracks = [
                {"index": t["index"], "audio": "waiting", "video": video} for t in data["tracks"]
            ]
        elif type == "track":
            self.tracks[data["index"]][data["kind"]] = data["status"]
        elif type == "end":
            self.status, self.error = data["status"], data["error"]
            self.finished_at = self.clock()

    def emit(self, type: str, data: dict) -> None:
        with self.cond:
            self._apply(type, data)
            self.events.append({"id": len(self.events), "type": type, "data": data})
            self.cond.notify_all()

    def set_status(self, status: JobStatus) -> None:
        with self.cond:
            self.status = status

    def finish(self, error: str | None = None, zip_path: Path | None = None) -> None:
        with self.cond:
            labels = {t["index"]: t["label"] for t in (self.playlist or {}).get("tracks", [])}
            missing = [
                {"index": t["index"], "label": labels[t["index"]], "kind": kind, "reason": s}
                for t in self.tracks
                for kind in ("audio", "video")
                if (s := t[kind]) in ("not_found", "failed")
            ]
            downloaded = sum(t["audio"] == "done" for t in self.tracks)
            self.zip_path = zip_path
        self.emit(
            "end",
            {
                "status": "error" if error else "done",
                "downloaded": downloaded,
                "missing": missing,
                "error": error,
                "zip_url": None if error else f"/api/jobs/{self.id}/zip",
            },
        )

    def snapshot(self) -> dict:
        with self.cond:
            return {
                "id": self.id,
                "status": self.status,
                "position": self.position,
                "playlist": self.playlist,
                "tracks": [dict(t) for t in self.tracks],
                "error": self.error,
                "zip_url": f"/api/jobs/{self.id}/zip" if self.zip_path else None,
            }


def build_zip(src_dir: Path, dest: Path) -> Path:
    """Zip a playlist folder under a root folder of the same name (the .m3u paths stay valid)."""
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_STORED) as z:
        for f in sorted(src_dir.iterdir()):
            if f.is_file():
                z.write(f, f"{src_dir.name}/{f.name}")
    return dest


def _run(job: Job, verbose: bool) -> None:
    cfg = job.cfg
    try:
        job.set_status("reading")
        session = make_session()
        playlist = read_playlist(job.url, cfg, session, job.name)
        job.emit("playlist", {**summarize(playlist), "videos": cfg.video_enabled})
        searcher = make_searcher(cfg.search_backend, cfg.searxng_url, session, verbose)
        downloader = Downloader(cfg.audio_quality, cfg.video_quality, verbose)
        files = job.dir / "files"
        print(f"[{job.id[:6]}] {playlist.name}: {len(playlist.tracks)} tracks", flush=True)
        sync_playlist(
            playlist,
            cfg,
            State.load(job.dir / "state.json"),
            searcher,
            downloader,
            session,
            files,
            log=lambda line: print(f"[{job.id[:6]}] {line}", flush=True),
            on_progress=lambda i, kind, status: job.emit(
                "track", {"index": i, "kind": kind, "status": status}
            ),
        )
        zip_path = build_zip(
            playlist_dir(files, playlist), job.dir / f"{sanitize_filename(playlist.name)}.zip"
        )
        job.finish(zip_path=zip_path)
    except JOB_ERRORS as e:
        job.finish(error=str(e))
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        job.finish(error=f"Unexpected error: {e}")


class JobManager:
    def __init__(
        self,
        base_dir: Path,
        workers: int = 1,
        ttl: float = 3600,
        max_queued: int = 5,
        verbose: bool = False,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.base_dir, self.ttl, self.max_queued = Path(base_dir), ttl, max_queued
        self.verbose, self.clock = verbose, clock
        self._jobs: dict[str, Job] = {}
        self._queue: deque[Job] = deque()
        self._cond = threading.Condition()
        self._stop = threading.Event()
        for _ in range(workers):
            threading.Thread(target=self._worker, daemon=True).start()
        threading.Thread(target=self._reaper, daemon=True).start()

    def create(self, url: object, body: dict) -> Job:
        url = validate_url(url)
        cfg, name = apply_options(body, load_config())
        with self._cond:
            if len(self._queue) >= self.max_queued:
                raise QueueFull(f"{self.max_queued} downloads are already waiting; try later")
            id = secrets.token_urlsafe(12)
            job = Job(id, url, cfg, name, self.base_dir / id, self.clock)
            self._jobs[id] = job
            self._queue.append(job)
            job.emit("queued", {"position": len(self._queue)})
            self._cond.notify()
        return job

    def get(self, id: str) -> Job | None:
        with self._cond:
            return self._jobs.get(id)

    def remove(self, id: str) -> None:
        with self._cond:
            job = self._jobs.pop(id, None)
        if job:
            shutil.rmtree(job.dir, ignore_errors=True)

    def reap(self, now: float | None = None) -> None:
        """Delete finished jobs older than ``ttl``. Running and waiting ones are never touched."""
        now = self.clock() if now is None else now
        with self._cond:
            expired = [
                j.id
                for j in self._jobs.values()
                if j.finished_at is not None and now - j.finished_at > self.ttl
            ]
        for id in expired:
            self.remove(id)

    def close(self) -> None:
        self._stop.set()
        with self._cond:
            self._cond.notify_all()
        shutil.rmtree(self.base_dir, ignore_errors=True)

    def _worker(self) -> None:
        while True:
            with self._cond:
                while not self._queue and not self._stop.is_set():
                    self._cond.wait()
                if self._stop.is_set():
                    return
                job = self._queue.popleft()
                for position, waiting in enumerate(self._queue, 1):
                    waiting.emit("queued", {"position": position})
            _run(job, self.verbose)

    def _reaper(self) -> None:
        while not self._stop.wait(60):
            self.reap()
