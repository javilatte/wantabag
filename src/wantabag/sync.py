"""Orchestration: read tracks -> find -> download -> tag -> .m3u and missing report."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import requests

from wantabag.config import Config
from wantabag.download import DownloadError
from wantabag.files import playlist_dir, track_stems, write_m3u, write_missing_report
from wantabag.matching import AUDIO, VIDEO, rank
from wantabag.models import Candidate, Playlist, Track
from wantabag.state import State
from wantabag.tagging import fetch_cover, write_tags

MAX_CANDIDATES = 3
Kind = Literal["audio", "video"]
ProgressFn = Callable[[int, Kind, str], None]


@dataclass
class Report:
    downloaded: int = 0
    skipped: int = 0
    missing: list[str] = field(default_factory=list)


@dataclass
class _Outcome:
    status: Literal["done", "not_found", "failed"]
    video_id: str | None = None
    error: str | None = None
    info: dict | None = None


def _direct_candidate(video_id: str) -> Candidate:
    return Candidate(
        video_id=video_id,
        url=f"https://www.youtube.com/watch?v={video_id}",
        title="",
        channel=None,
        duration_s=None,
        origin="youtube",
    )


def _acquire(
    kind: Kind,
    track: Track,
    dest: Path,
    known_id: str | None,
    searcher,
    downloader,
    notify: Callable[[str], None],
) -> _Outcome:
    """Download one file: reuse a known video id, else search and try the best candidates."""
    download = downloader.audio if kind == "audio" else downloader.video
    if known_id:
        cands = [_direct_candidate(known_id)]
    else:
        notify("searching")
        found = searcher.search(track, kind)
        cands = [c for _, c in rank(track, found, AUDIO if kind == "audio" else VIDEO)]
        cands = cands[:MAX_CANDIDATES]
        if not cands:
            return _Outcome("not_found")
    error = ""
    for cand in cands:
        notify("downloading")
        try:
            info = download(cand, track, dest)
        except DownloadError as e:
            error = str(e)
            continue
        return _Outcome("done", cand.video_id, info=info)
    return _Outcome("failed", error=error)


def sync_playlist(
    playlist: Playlist,
    cfg: Config,
    state: State,
    searcher,
    downloader,
    session: requests.Session,
    out_dir: Path,
    limit: int | None = None,
    log: Callable[[str], None] = print,
    on_progress: ProgressFn | None = None,
) -> Report:
    directory = playlist_dir(out_dir, playlist)
    stems = track_stems(playlist)
    total = len(playlist.tracks)
    report = Report()
    entries: list[tuple[Track, Path]] = []
    kinds: list[Kind] = ["audio", "video"] if cfg.video_enabled else ["audio"]

    for i, track in enumerate(playlist.tracks):
        mp3 = directory / f"{stems[i]}.mp3"
        entries.append((track, mp3))
        if limit is not None and i >= limit:
            continue
        key, prefix = track.key(), f"[{i + 1}/{total}] {track.label}"
        status_parts: list[str] = []

        for kind in kinds:
            dest = mp3 if kind == "audio" else mp3.with_suffix(".mp4")
            field_status = "status" if kind == "audio" else "video_status"
            field_id = "video_id" if kind == "audio" else "video_video_id"

            def notify(status: str, i=i, kind=kind) -> None:
                if on_progress:
                    on_progress(i, kind, status)

            if dest.exists():
                report.skipped += 1
                status_parts.append(f"{kind}: skip")
                notify("skipped")
                continue
            entry = state.get(key)
            outcome = _acquire(kind, track, dest, entry.get(field_id), searcher, downloader, notify)
            if outcome.status == "done" and kind == "audio":
                _tag(mp3, track, i + 1, total, outcome, session)
            state.update(
                key,
                **{
                    field_status: outcome.status,
                    field_id: outcome.video_id or entry.get(field_id),
                    "error": outcome.error,
                    "attempts": state.bump_attempts(key) if outcome.status != "done" else 0,
                },
            )
            notify(outcome.status)
            if outcome.status == "done":
                report.downloaded += 1
                status_parts.append(f"{kind}: ok")
            else:
                reason = (
                    "not found" if outcome.status == "not_found" else f"failed: {outcome.error}"
                )
                report.missing.append(f"{track.label}  [{kind} {reason}]")
                status_parts.append(f"{kind}: {reason}")
        log(f"{prefix} ... {', '.join(status_parts)}")

    write_m3u(directory, playlist.name, entries)
    path = write_missing_report(directory, report.missing)
    if path:
        log(f"{len(report.missing)} item(s) could not be downloaded; see {path}")
    return report


def _tag(path: Path, track: Track, position: int, total: int, outcome: _Outcome, session) -> None:
    info = outcome.info or {}
    thumbs = info.get("thumbnails") or []
    cover_url = (
        track.artwork_url or info.get("thumbnail") or (thumbs[-1].get("url") if thumbs else None)
    )
    album = track.album or info.get("album")
    write_tags(
        path,
        track,
        position,
        total,
        album,
        fetch_cover(session, cover_url),
        f"https://www.youtube.com/watch?v={outcome.video_id}",
    )
