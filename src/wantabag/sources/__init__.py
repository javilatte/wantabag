"""Playlist readers. They only read metadata; nothing is ever downloaded from them."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Literal
from urllib.parse import urlparse

if TYPE_CHECKING:
    import requests

    from wantabag.config import Config
    from wantabag.models import Playlist

UNSUPPORTED = "Only Apple Music and Spotify playlist URLs are supported"

_APPLE_RE = re.compile(r"^/(?P<sf>[a-z]{2})/playlist(?:/[^/]+)?/(?P<id>pl\.[\w.-]+)/?$", re.I)
_SPOTIFY_PATH_RE = re.compile(r"^(?:/intl-[\w-]+)?/playlist/(?P<id>[A-Za-z0-9]+)/?$")
_SPOTIFY_URI_RE = re.compile(r"^spotify:playlist:(?P<id>[A-Za-z0-9]+)$")


@dataclass(frozen=True)
class PlaylistRef:
    source: Literal["apple", "spotify"]
    id: str
    storefront: str = ""


def parse_playlist_url(url: str) -> PlaylistRef:
    url = url.strip()
    if m := _SPOTIFY_URI_RE.match(url):
        return PlaylistRef("spotify", m["id"])
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host == "music.apple.com" and (m := _APPLE_RE.match(parsed.path)):
        return PlaylistRef("apple", m["id"], m["sf"].lower())
    if host == "open.spotify.com" and (m := _SPOTIFY_PATH_RE.match(parsed.path)):
        return PlaylistRef("spotify", m["id"])
    raise ValueError(UNSUPPORTED)


def read_playlist(url: str, cfg: Config, session: requests.Session, name: str = "") -> Playlist:
    """Read a playlist; a non-empty ``name`` overrides the one the source serves."""
    ref = parse_playlist_url(url)
    if ref.source == "apple":
        from wantabag.sources import apple

        playlist = apple.read(url, ref, session)
    else:
        from wantabag.sources import spotify

        playlist = spotify.read(url, ref, cfg, session)
    return replace(playlist, name=name) if name else playlist
