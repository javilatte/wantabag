"""Spotify playlist reader.

It reads the public embed page (first ~100 tracks, no ISRC or album). The Web API is used only
with the user's own client id/secret from the config; without them, or if the API fails, the
embed tracks are kept and we warn when the list looks truncated.
Note: since March 2026 Spotify only returns playlist items for playlists the API user owns,
so the API is best effort.
"""

from __future__ import annotations

import json
import re
import sys
from typing import Any

import requests

from wantabag.config import Config
from wantabag.http import page_text
from wantabag.models import Playlist, Track
from wantabag.sources import PlaylistRef

EMBED_URL = "https://open.spotify.com/embed/playlist/{id}"
TOKEN_URL = "https://accounts.spotify.com/api/token"
API_URL = "https://api.spotify.com/v1/playlists/{id}/items?limit=100"
EMBED_LIMIT = 100
TRUNCATED_HINT = (
    "Playlist may be truncated; add Spotify API credentials with 'wantabag setup' "
    "or config [spotify]"
)
_NEXT_DATA_RE = re.compile(
    r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL | re.IGNORECASE
)


class SpotifyError(Exception):
    pass


def warn(msg: str) -> None:
    print(f"Warning: {msg}", file=sys.stderr)


def _dig(data: Any, *path: str) -> Any:
    for key in path:
        if not isinstance(data, dict):
            return None
        data = data.get(key)
    return data


def parse_embed(html: str) -> tuple[str, list[Track]]:
    """Return (name, tracks) from the embed page."""
    m = _NEXT_DATA_RE.search(html)
    if not m:
        raise SpotifyError("embed data not found; the page format may have changed")
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError as e:
        raise SpotifyError("embed data is not valid JSON") from e
    state = _dig(data, "props", "pageProps", "state") or {}
    entity = _dig(state, "data", "entity") or {}
    cover = next((s.get("url") for s in _dig(entity, "coverArt", "sources") or []), None)
    tracks = []
    for it in entity.get("trackList") or []:
        title, subtitle = it.get("title"), it.get("subtitle") or ""
        if not title:
            continue
        artists = tuple(a.strip() for a in re.split(r",| ", subtitle) if a.strip())
        tracks.append(
            Track(
                title=title,
                artists=artists,
                duration_ms=it.get("duration"),
                artwork_url=cover,
                source_id=str(it.get("uri", "")).rsplit(":", 1)[-1],
                artist_display=re.sub(r"\s+", " ", subtitle.replace(" ", " ")).strip(),
            )
        )
    return str(entity.get("name") or ""), tracks


def _api_track(item: dict) -> Track | None:
    if not item or not item.get("name") or item.get("type", "track") != "track":
        return None
    artists = tuple(a["name"] for a in item.get("artists") or [] if a.get("name"))
    images = _dig(item, "album", "images") or []
    return Track(
        title=item["name"],
        artists=artists,
        album=_dig(item, "album", "name"),
        duration_ms=item.get("duration_ms"),
        isrc=_dig(item, "external_ids", "isrc"),
        artwork_url=images[0].get("url") if images else None,
        source_id=str(item.get("id", "")),
    )


def fetch_client_token(session: requests.Session, client_id: str, client_secret: str) -> str:
    resp = session.post(
        TOKEN_URL,
        data={"grant_type": "client_credentials"},
        auth=(client_id, client_secret),
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def fetch_api_tracks(session: requests.Session, playlist_id: str, token: str) -> list[Track]:
    headers = {"Authorization": f"Bearer {token}"}
    url: str | None = API_URL.format(id=playlist_id)
    tracks: list[Track] = []
    while url:
        resp = session.get(url, headers=headers)
        resp.raise_for_status()
        body = resp.json()
        for entry in body.get("items", []):
            # The renamed endpoint returns "item"; older responses used "track".
            if t := _api_track(entry.get("item") or entry.get("track")):
                tracks.append(t)
        url = body.get("next")
    return tracks


def read(url: str, ref: PlaylistRef, cfg: Config, session: requests.Session) -> Playlist:
    page = session.get(EMBED_URL.format(id=ref.id))
    if page.status_code == 404:
        raise SpotifyError("playlist not found (is it public?)")
    page.raise_for_status()
    name, tracks = parse_embed(page_text(page))

    complete = len(tracks) < EMBED_LIMIT
    if cfg.spotify_client_id and cfg.spotify_client_secret:
        try:
            token = fetch_client_token(session, cfg.spotify_client_id, cfg.spotify_client_secret)
            full = fetch_api_tracks(session, ref.id, token)
            if len(full) >= len(tracks):
                tracks, complete = full, True
        except (requests.RequestException, KeyError, ValueError):
            pass
    if not complete:
        warn(TRUNCATED_HINT)

    if not tracks:
        raise SpotifyError("no tracks found; the playlist may be private or empty")
    return Playlist("spotify", ref.id, url, name or ref.id, tuple(tracks), len(tracks))
