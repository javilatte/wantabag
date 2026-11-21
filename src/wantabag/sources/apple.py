"""Apple Music playlist reader.

Reads only the public data embedded in the playlist page (title, artists, album, duration,
artwork). No account, key or private API is used, so there are no ISRCs. If the page declares
more songs than it carries, the reader warns that the list may be incomplete.
"""

from __future__ import annotations

import json
import re
import sys
from typing import Any

import requests

from wantabag.http import page_text
from wantabag.models import Playlist, Track
from wantabag.sources import PlaylistRef

_DATA_RE = re.compile(
    r'<script[^>]*id="serialized-server-data"[^>]*>(.*?)</script>', re.DOTALL | re.IGNORECASE
)
_JSON_LD_RE = re.compile(r'<script type="application/ld\+json"[^>]*>(.*?)</script>', re.DOTALL)
_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.DOTALL | re.IGNORECASE)


class AppleError(Exception):
    pass


def warn(msg: str) -> None:
    print(f"Warning: {msg}", file=sys.stderr)


def _walk(node: Any):
    """Yield every dict and list in a JSON tree."""
    yield node
    if isinstance(node, dict):
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def _artwork(url: str | None) -> str | None:
    if not url:
        return None
    return (
        url.replace("{w}", "600").replace("{h}", "600").replace("{c}", "bb").replace("{f}", "jpg")
    )


def _html_track(item: dict) -> Track | None:
    title = item.get("title")
    if not title:
        return None
    artist_display = item.get("artistName") or ""
    artists = tuple(
        link["title"] for link in item.get("subtitleLinks") or [] if link.get("title")
    ) or ((artist_display,) if artist_display else ())
    tertiary = item.get("tertiaryLinks") or []
    album = tertiary[0].get("title") if tertiary else None
    ids = (item.get("contentDescriptor") or {}).get("identifiers") or {}
    return Track(
        title=title,
        artists=artists,
        album=album,
        duration_ms=item.get("duration"),
        artwork_url=_artwork(((item.get("artwork") or {}).get("dictionary") or {}).get("url")),
        source_id=str(ids.get("storeAdamID", "")),
        artist_display=artist_display,
    )


def parse_html(html: str) -> tuple[str, int | None, list[Track]]:
    """Return (name, declared_count, tracks) from the public playlist page."""
    name, declared, tracks = "", None, []

    if m := _DATA_RE.search(html):
        try:
            data = json.loads(m.group(1))
        except json.JSONDecodeError:
            data = None
        for node in _walk(data):
            if not isinstance(node, dict) or not isinstance(node.get("items"), list):
                continue
            if node.get("itemKind") == "trackLockup":
                tracks = [t for it in node["items"] if (t := _html_track(it))]
            elif node.get("itemKind") == "containerDetailHeaderLockup" and node["items"]:
                name = name or str(node["items"][0].get("title") or "")

    for m in _JSON_LD_RE.finditer(html):
        try:
            ld = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        if isinstance(ld, dict) and "MusicPlaylist" in str(ld.get("@type")):
            name = name or str(ld.get("name") or "")
            if isinstance(ld.get("numTracks"), int):
                declared = ld["numTracks"]
    if not name and (m := _TITLE_RE.search(html)):
        name = re.split(r"\s+(?:by|de|-)\s+|\s+on Apple Music|\s+en Apple Music", m.group(1))[
            0
        ].strip()
    return name, declared, tracks


def read(url: str, ref: PlaylistRef, session: requests.Session) -> Playlist:
    page = session.get(url)
    if page.status_code == 404:
        raise AppleError("playlist not found (is it public?)")
    page.raise_for_status()
    name, declared, tracks = parse_html(page_text(page))
    if not tracks:
        raise AppleError("no tracks found; the page format may have changed or the list is empty")
    if declared and len(tracks) < declared:
        warn(
            f"Apple only shows {len(tracks)} of {declared} songs on the public page; "
            "the list may be incomplete"
        )
    return Playlist("apple", ref.id, url, name or ref.id, tuple(tracks), declared or len(tracks))
