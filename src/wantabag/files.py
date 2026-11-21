"""File naming, .m3u playlists and the missing-songs report."""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

from wantabag.models import Playlist, Track

MAX_NAME_BYTES = 180
MISSING_REPORT = "wantabag-missing.txt"
_FORBIDDEN = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
_RESERVED = re.compile(r"^(con|prn|aux|nul|com[1-9]|lpt[1-9])(\..*)?$", re.IGNORECASE)


def _truncate(text: str, max_bytes: int) -> str:
    return text.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")


def sanitize_filename(name: str, max_bytes: int = MAX_NAME_BYTES) -> str:
    """Make ``name`` a safe single path component on any common filesystem."""
    name = unicodedata.normalize("NFC", name)
    name = _FORBIDDEN.sub("_", name)
    name = re.sub(r"\s+", " ", name).strip()
    name = _truncate(name, max_bytes).rstrip(". ")
    if _RESERVED.match(name):
        name += "_"
    return name or "Unknown"


def playlist_dir(out_dir: Path, playlist: Playlist) -> Path:
    return out_dir / sanitize_filename(playlist.name)


def track_stems(playlist: Playlist) -> dict[int, str]:
    """File stem per playlist position; repeated names get a `` (2)`` suffix."""
    seen: dict[str, int] = {}
    stems: dict[int, str] = {}
    for i, t in enumerate(playlist.tracks):
        stem = sanitize_filename(t.label, MAX_NAME_BYTES - 12)
        seen[stem.casefold()] = seen.get(stem.casefold(), 0) + 1
        n = seen[stem.casefold()]
        stems[i] = stem if n == 1 else f"{stem} ({n})"
    return stems


def write_m3u(directory: Path, name: str, entries: list[tuple[Track, Path]]) -> Path:
    """Write ``<name>.m3u`` listing the files that exist, in playlist order."""
    lines = ["#EXTM3U"]
    for track, path in entries:
        if path.exists():
            rel = path.relative_to(directory).as_posix()
            lines.append(
                f"#EXTINF:{track.duration_s if track.duration_s is not None else -1},{track.label}"
            )
            lines.append(rel)
    out = directory / f"{sanitize_filename(name)}.m3u"
    directory.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


def write_missing_report(directory: Path, lines: list[str]) -> Path | None:
    """Write the report, or remove a stale one when nothing is missing."""
    path = directory / MISSING_REPORT
    if not lines:
        path.unlink(missing_ok=True)
        return None
    directory.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
