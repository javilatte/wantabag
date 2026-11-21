"""Downloading with yt-dlp (audio -> MP3, optional mp4 video) and the ffmpeg check."""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

from wantabag.config import AUDIO_QUALITIES, VIDEO_QUALITIES
from wantabag.matching import AUDIO, VIDEO, Profile, duration_tolerance
from wantabag.models import Candidate, Track

FFMPEG_HELP = """ffmpeg is required but was not found. Install it with one of:
  Debian/Ubuntu:  sudo apt install ffmpeg
  Fedora:         sudo dnf install ffmpeg-free
  Arch:           sudo pacman -S ffmpeg
  macOS (brew):   brew install ffmpeg
  Windows:        winget install Gyan.FFmpeg"""


class DownloadError(Exception):
    pass


class DurationMismatch(DownloadError):
    pass


def check_ffmpeg() -> str | None:
    """Return an install hint if ffmpeg or ffprobe is missing, else None."""
    if shutil.which("ffmpeg") and shutil.which("ffprobe"):
        return None
    return FFMPEG_HELP


def js_runtimes() -> dict[str, dict]:
    """First available JS runtime for yt-dlp's YouTube extraction (deno extra, node, bun)."""
    try:
        import deno

        return {"deno": {"path": deno.find_deno_bin()}}
    except Exception:  # noqa: BLE001
        pass
    for name in ("deno", "node", "bun"):
        if path := shutil.which(name):
            return {name: {"path": path}}
    return {}


class SilentLogger:
    """yt-dlp prints errors to stderr even when quiet unless a logger is set.

    Real failures still reach us as exceptions and end up in the missing report.
    """

    def debug(self, msg: str) -> None:
        pass

    info = warning = error = debug


def base_opts(verbose: bool = False) -> dict[str, Any]:
    opts: dict[str, Any] = {"quiet": not verbose, "no_warnings": not verbose, "noprogress": True}
    if not verbose:
        opts["logger"] = SilentLogger()
    if runtimes := js_runtimes():
        opts["js_runtimes"] = runtimes
    return opts


def _default_factory(opts: dict[str, Any]):
    import yt_dlp

    return yt_dlp.YoutubeDL(opts)


def audio_format_opts(quality: str) -> dict[str, Any]:
    return {
        "format": "bestaudio/best",
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": AUDIO_QUALITIES[quality][0],
            }
        ],
    }


def video_format(quality: str) -> str:
    h = VIDEO_QUALITIES[quality]
    if h is None:
        return "bv*+ba/b"
    return f"bv*[height<={h}][ext=mp4]+ba[ext=m4a]/bv*[height<={h}]+ba/b[height<={h}]/b"


class Downloader:
    def __init__(
        self,
        audio_quality: str,
        video_quality: str,
        verbose: bool = False,
        ydl_factory: Callable[[dict], Any] = _default_factory,
    ) -> None:
        self.audio_quality = audio_quality
        self.video_quality = video_quality
        self.verbose = verbose
        self.factory = ydl_factory

    def _fetch(
        self, cand: Candidate, track: Track, dest: Path, extra: dict, profile: Profile
    ) -> dict:
        """Check the real duration first, then download into a temp name and rename."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp_base = dest.with_name(dest.stem + ".wantabag-tmp")
        opts = {**base_opts(self.verbose), **extra, "outtmpl": f"{tmp_base}.%(ext)s"}
        try:
            with self.factory(opts) as ydl:
                info = ydl.extract_info(cand.url, download=False)
                real, wanted = info.get("duration"), track.duration_s
                if real and wanted and abs(real - wanted) > duration_tolerance(wanted, profile):
                    raise DurationMismatch(f"duration {real}s vs expected {wanted}s")
                info = ydl.process_ie_result(info, download=True)
            produced = dest.with_name(tmp_base.name + dest.suffix)
            if not produced.exists():
                raise DownloadError("yt-dlp produced no output file")
            os.replace(produced, dest)
            return info
        except DownloadError:
            raise
        except Exception as e:  # yt-dlp raises its own DownloadError/ExtractorError family
            raise DownloadError(str(e)) from e
        finally:
            for leftover in dest.parent.iterdir():
                if leftover.name.startswith(tmp_base.name + "."):
                    leftover.unlink(missing_ok=True)

    def audio(self, cand: Candidate, track: Track, dest: Path) -> dict:
        return self._fetch(cand, track, dest, audio_format_opts(self.audio_quality), AUDIO)

    def video(self, cand: Candidate, track: Track, dest: Path) -> dict:
        extra = {"format": video_format(self.video_quality), "merge_output_format": "mp4"}
        return self._fetch(cand, track, dest, extra, VIDEO)
