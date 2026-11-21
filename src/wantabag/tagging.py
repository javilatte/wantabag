"""ID3 tags for the downloaded MP3 files."""

from __future__ import annotations

from pathlib import Path

import requests
from mutagen.id3 import APIC, ID3, TALB, TIT2, TPE1, TRCK, TSRC, TXXX

from wantabag.models import Track


def fetch_cover(session: requests.Session, url: str | None) -> tuple[bytes, str] | None:
    if not url:
        return None
    try:
        resp = session.get(url)
        resp.raise_for_status()
    except requests.RequestException:
        return None
    mime = resp.headers.get("Content-Type", "image/jpeg").split(";")[0].strip()
    return resp.content, mime if mime.startswith("image/") else "image/jpeg"


def write_tags(
    path: Path,
    track: Track,
    position: int,
    total: int,
    album: str | None,
    cover: tuple[bytes, str] | None,
    source_url: str,
) -> None:
    tags = ID3()
    tags.add(TIT2(encoding=3, text=track.title))
    tags.add(TPE1(encoding=3, text=track.artist_display))
    if album:
        tags.add(TALB(encoding=3, text=album))
    tags.add(TRCK(encoding=3, text=f"{position}/{total}"))
    if track.isrc:
        tags.add(TSRC(encoding=3, text=track.isrc))
    if cover:
        data, mime = cover
        tags.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=data))
    tags.add(TXXX(encoding=3, desc="wantabag_source", text=source_url))
    tags.save(path, v2_version=3)
