import json
import re

import pytest
import requests

from tests.fakes import FakeResponse, FakeSession, fixture_text
from wantabag.config import Config
from wantabag.sources import parse_playlist_url, read_playlist

APPLE_URL = "https://music.apple.com/es/playlist/test-favorites/pl.u-TEST"
UTF8_HEADERS = {"content-type": "text/html"}


@pytest.mark.parametrize(
    ("url", "source", "pid"),
    [
        (
            "https://music.apple.com/es/playlist/mis-favoritas/pl.u-Ab12Cd34Ef56",
            "apple",
            "pl.u-Ab12Cd34Ef56",
        ),
        ("https://music.apple.com/us/playlist/pl.u-abc?l=en", "apple", "pl.u-abc"),
        (
            "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M?si=xyz",
            "spotify",
            "37i9dQZF1DXcBWIGoYBM5M",
        ),
        ("https://open.spotify.com/intl-es/playlist/AbC123", "spotify", "AbC123"),
        ("spotify:playlist:AbC123", "spotify", "AbC123"),
    ],
)
def test_parse_valid_urls(url, source, pid):
    ref = parse_playlist_url(url)
    assert (ref.source, ref.id) == (source, pid)


@pytest.mark.parametrize(
    "url",
    [
        "https://music.apple.com/es/album/foo/123",
        "https://open.spotify.com/album/abc",
        "https://example.com/playlist/abc",
        "not a url",
    ],
)
def test_parse_invalid_urls(url):
    with pytest.raises(ValueError, match="Only Apple Music and Spotify playlist URLs"):
        parse_playlist_url(url)


def test_apple_reads_the_public_page_only():
    session = FakeSession({APPLE_URL: FakeResponse(fixture_text("apple_playlist.html"))})
    pl = read_playlist(APPLE_URL, Config(), session)
    assert pl.name == "Test Favorites"
    assert [t.title for t in pl.tracks] == [
        "Song One",
        "Canción Dos (feat. Guest)",
        "Third / Song",
        "Fourth",
        "Fifth",
    ]
    assert pl.tracks[1].artists == ("Artist B", "Artist C")
    assert pl.tracks[1].album == "Álbum Dos"
    assert pl.tracks[0].artwork_url.endswith("600x600bb.jpg")
    assert all(t.isrc is None for t in pl.tracks)
    assert pl.declared_count == 5
    assert session.urls() == [APPLE_URL]


def test_apple_warns_when_page_has_fewer_songs_than_declared(capsys):
    html = fixture_text("apple_playlist.html").replace('"numTracks": 5', '"numTracks": 240')
    pl = read_playlist(APPLE_URL, Config(), FakeSession({APPLE_URL: FakeResponse(html)}))
    assert len(pl.tracks) == 5
    assert "Apple only shows 5 of 240 songs" in capsys.readouterr().err


def test_apple_complete_page_does_not_warn(capsys):
    read_playlist(
        APPLE_URL,
        Config(),
        FakeSession({APPLE_URL: FakeResponse(fixture_text("apple_playlist.html"))}),
    )
    assert capsys.readouterr().err == ""


def test_apple_utf8_without_charset_keeps_accents():
    html = (
        '<html><head><script type="application/ld+json">'
        '{"@type": "MusicPlaylist", "name": "Canciones", "numTracks": 1}</script></head><body>'
        '<script type="application/json" id="serialized-server-data">'
        '[{"data": {"sections": [{"itemKind": "trackLockup", "items": ['
        '{"title": "París", "artistName": "Belén Aguilera", '
        '"subtitleLinks": [{"title": "Belén Aguilera"}]}]}]}}]</script></body></html>'
    )
    resp = FakeResponse(html.encode("utf-8"), headers=UTF8_HEADERS)
    assert "Ã" in resp.text
    pl = read_playlist(APPLE_URL, Config(), FakeSession({APPLE_URL: resp}))
    assert pl.tracks[0].label == "Belén Aguilera - París"


def test_apple_missing_playlist():
    session = FakeSession({APPLE_URL: FakeResponse("", 404)})
    with pytest.raises(Exception, match="not found"):
        read_playlist(APPLE_URL, Config(), session)


SP_URL = "https://open.spotify.com/playlist/PL123"
EMBED = "https://open.spotify.com/embed/playlist/PL123"
SP_P1 = "https://api.spotify.com/v1/playlists/PL123/items?limit=100"
SP_P2 = "https://api.spotify.com/v1/playlists/PL123/items?offset=2&limit=2"


def test_spotify_embed_without_credentials():
    session = FakeSession({EMBED: FakeResponse(fixture_text("spotify_embed.html"))})
    pl = read_playlist(SP_URL, Config(), session)
    assert pl.name == "Test Spotify List"
    assert [(t.title, t.artists) for t in pl.tracks] == [
        ("Song One", ("Artist A", "Artist B")),
        ("Song Two", ("Artist C",)),
    ]
    assert pl.tracks[0].artist_display == "Artist A, Artist B"
    assert pl.tracks[0].artwork_url == "https://i.scdn.co/image/fake"
    assert session.urls() == [EMBED]


def test_spotify_api_with_credentials_and_pagination():
    session = FakeSession(
        {
            EMBED: FakeResponse(fixture_text("spotify_embed.html")),
            "https://accounts.spotify.com/api/token": FakeResponse('{"access_token": "T"}'),
            SP_P1: FakeResponse(fixture_text("spotify_api_p1.json")),
            SP_P2: FakeResponse(fixture_text("spotify_api_p2.json")),
        }
    )
    cfg = Config(spotify_client_id="id", spotify_client_secret="secret")
    pl = read_playlist(SP_URL, cfg, session)
    assert [t.title for t in pl.tracks] == ["Song One", "Song Two", "Song Three"]
    assert pl.tracks[0].isrc == "ISRC1"
    assert pl.tracks[0].album == "Album 1"
    assert session.calls[-1][1]["headers"] == {"Authorization": "Bearer T"}


def test_spotify_api_failure_keeps_embed_tracks_without_warning_when_short(capsys):
    session = FakeSession(
        {
            EMBED: FakeResponse(fixture_text("spotify_embed.html")),
            "https://accounts.spotify.com/api/token": FakeResponse("", 400),
        }
    )
    cfg = Config(spotify_client_id="id", spotify_client_secret="bad")
    pl = read_playlist(SP_URL, cfg, session)
    assert len(pl.tracks) == 2
    assert capsys.readouterr().err == ""


def _embed_html(n_tracks: int, **entity_extra) -> str:
    html = fixture_text("spotify_embed.html")
    data = json.loads(re.search(r'type="application/json">(.*)</script>', html).group(1))
    state = data["props"]["pageProps"]["state"]
    entity = state["data"]["entity"]
    entity["trackList"] = [
        {"uri": f"spotify:track:{i}", "title": f"T{i}", "subtitle": "A", "duration": 1000}
        for i in range(n_tracks)
    ]
    entity.update(entity_extra)
    state.setdefault("settings", {})["session"] = {"accessToken": "ANON"}
    return f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script>'


def test_spotify_without_credentials_never_uses_the_web_api(capsys):
    session = FakeSession({EMBED: FakeResponse(_embed_html(100))})
    pl = read_playlist(SP_URL, Config(), session)
    assert len(pl.tracks) == 100
    assert session.urls() == [EMBED]
    assert "Playlist may be truncated" in capsys.readouterr().err


def test_spotify_full_embed_warns_when_api_fails(capsys):
    session = FakeSession(
        {
            EMBED: FakeResponse(_embed_html(100)),
            "https://accounts.spotify.com/api/token": FakeResponse('{"access_token": "T"}'),
            SP_P1: lambda kw: (_ for _ in ()).throw(requests.ConnectionError("x")),
        }
    )
    cfg = Config(spotify_client_id="id", spotify_client_secret="secret")
    pl = read_playlist(SP_URL, cfg, session)
    assert len(pl.tracks) == 100
    assert "Playlist may be truncated" in capsys.readouterr().err


def test_spotify_utf8_without_charset_keeps_accents():
    html = _embed_html(1, name="Canciones").replace("T0", "París").replace('"A"', '"Belén"')
    resp = FakeResponse(html.encode("utf-8"), headers=UTF8_HEADERS)
    pl = read_playlist(SP_URL, Config(), FakeSession({EMBED: resp}))
    assert pl.tracks[0].title == "París"
