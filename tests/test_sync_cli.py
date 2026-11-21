from pathlib import Path

import pytest
from mutagen.id3 import ID3

from tests.fakes import FakeDownloader, FakeResponse, FakeSearcher, FakeSession, fixture_text
from wantabag import cli
from wantabag.config import Config, PlaylistEntry, load_config, save_config
from wantabag.state import State, run_lock
from wantabag.sync import sync_playlist

URL = "https://music.apple.com/es/playlist/test-favorites/pl.u-TEST"


@pytest.fixture
def wired(monkeypatch, tmp_path):
    session = FakeSession(
        {
            URL: FakeResponse(fixture_text("apple_playlist.html")),
            "http://is1-ssl.mzstatic.com/x": FakeResponse(b"x"),
        }
    )

    class Session(FakeSession):
        def get(self, url, **kw):
            if "mzstatic.com/image" in url:
                self.calls.append((url, kw))
                return FakeResponse(b"\xff\xd8img", headers={"Content-Type": "image/jpeg"})
            return super().get(url, **kw)

    session.__class__ = Session
    searcher, downloader = FakeSearcher(missing={"Fourth"}), FakeDownloader()
    monkeypatch.setattr(cli, "make_session", lambda: session)
    monkeypatch.setattr(cli, "make_searcher", lambda *a, **k: searcher)
    monkeypatch.setattr(cli, "Downloader", lambda *a, **k: downloader)
    monkeypatch.setattr(cli, "check_ffmpeg", lambda: None)
    return searcher, downloader, tmp_path / "out"


def test_get_downloads_tags_and_reports(wired, capsys):
    searcher, downloader, out = wired
    assert cli.main(["get", URL, "--out", str(out)]) == 0
    folder = out / "Test Favorites"
    names = sorted(p.name for p in folder.iterdir())
    assert names == [
        "Artist A - Song One.mp3",
        "Artist A - Third _ Song.mp3",
        "Artist B & Artist C - Canción Dos (feat. Guest).mp3",
        "Artist E - Fifth.mp3",
        "Test Favorites.m3u",
        "wantabag-missing.txt",
    ]
    tags = ID3(folder / "Artist A - Song One.mp3")
    assert tags["TRCK"].text == ["1/5"]
    assert "TSRC" not in tags
    assert tags["TALB"].text == ["Album One"]
    assert tags["APIC:Cover"].data == b"\xff\xd8img"
    assert ID3(folder / "Artist E - Fifth.mp3")["TRCK"].text == ["5/5"]
    assert "Artist D - Fourth  [audio not found]" in (folder / "wantabag-missing.txt").read_text()
    m3u = (folder / "Test Favorites.m3u").read_text(encoding="utf-8")
    assert "Artist D" not in m3u and m3u.count("#EXTINF") == 4
    assert "[4/5] Artist D - Fourth ... audio: not found" in capsys.readouterr().out


def test_second_sync_does_not_search_again_for_existing_files(wired):
    searcher, downloader, out = wired
    cfg = Config(output_dir=str(out))
    save_config(cfg)
    assert cli.main(["add", URL]) == 0
    assert cli.main(["sync"]) == 0
    first = len(searcher.calls)
    assert first == 5
    assert cli.main(["sync"]) == 0
    assert searcher.calls[first:] == [("Fourth", "audio")]
    assert load_config().playlists[0].name == "Test Favorites"


def test_failed_download_is_retried_with_a_fresh_search(wired):
    searcher, downloader, out = wired
    downloader.fail = {"Song One"}
    assert cli.main(["get", URL, "--out", str(out), "--limit", "1"]) == 0
    key = "t:artist a|song one"
    assert State.load().get(key)["status"] == "failed"
    assert not (out / "Test Favorites" / "Artist A - Song One.mp3").exists()
    downloader.fail = set()
    assert cli.main(["get", URL, "--out", str(out), "--limit", "1"]) == 0
    assert State.load().get(key)["status"] == "done"
    assert (out / "Test Favorites" / "Artist A - Song One.mp3").exists()
    assert len(searcher.calls) == 2


def test_deleted_file_is_redownloaded_from_stored_video_id(wired):
    searcher, downloader, out = wired
    assert cli.main(["get", URL, "--out", str(out), "--limit", "1"]) == 0
    mp3 = out / "Test Favorites" / "Artist A - Song One.mp3"
    mp3.unlink()
    assert cli.main(["get", URL, "--out", str(out), "--limit", "1"]) == 0
    assert mp3.exists()
    assert len(searcher.calls) == 1
    assert downloader.calls[0] == downloader.calls[1]


def test_dry_run_lists_tracks_without_searching(wired, capsys):
    searcher, downloader, out = wired
    assert cli.main(["get", URL, "--dry-run"]) == 0
    text = capsys.readouterr().out
    assert "Test Favorites: 5 tracks" in text and "5. Artist E - Fifth" in text
    assert searcher.calls == [] and downloader.calls == []


def test_video_enabled_downloads_mp4_next_to_mp3(wired):
    searcher, downloader, out = wired
    assert cli.main(["get", URL, "--out", str(out), "--limit", "1", "--video"]) == 0
    assert (out / "Test Favorites" / "Artist A - Song One.mp4").exists()
    assert tuple({k for k, _ in downloader.calls} - {"audio"}) == ("video",)


def test_add_list_remove(wired, capsys):
    cfg_out = wired[2]
    save_config(Config(output_dir=str(cfg_out)))
    assert cli.main(["add", URL]) == 0
    assert cli.main(["add", URL]) == 1
    capsys.readouterr()
    assert cli.main(["list"]) == 0
    assert "1. [apple] Test Favorites - 0 files" in capsys.readouterr().out
    assert cli.main(["remove", "1"]) == 0
    assert load_config().playlists == []
    assert cli.main(["remove", "1"]) == 1


def test_busy_lock_gives_message_and_exit_1(wired, capsys):
    with run_lock():
        assert cli.main(["get", URL, "--out", str(wired[2])]) == 1
    assert "another wantabag run is in progress" in capsys.readouterr().err


def test_missing_ffmpeg_is_explained(wired, monkeypatch, capsys):
    monkeypatch.setattr(cli, "check_ffmpeg", lambda: "ffmpeg is required but was not found.")
    assert cli.main(["get", URL, "--out", str(wired[2])]) == 1
    assert "ffmpeg is required" in capsys.readouterr().err


def test_invalid_url_is_a_clean_error(capsys):
    assert cli.main(["get", "https://example.com/x"]) == 1
    assert "Only Apple Music and Spotify playlist URLs" in capsys.readouterr().err


def test_version(capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["--version"])
    assert e.value.code == 0
    assert "wantabag 0.1.0" in capsys.readouterr().out


def test_output_dir_with_tilde_expands(tmp_path: Path):
    assert Config(output_dir="~/x").out_path == Path.home() / "x"


def test_get_name_sets_folder_and_m3u(wired):
    _, _, out = wired
    assert cli.main(["get", URL, "--out", str(out), "--limit", "1", "--name", "Mis favoritas"]) == 0
    assert (out / "Mis favoritas" / "Artist A - Song One.mp3").exists()
    assert (out / "Mis favoritas" / "Mis favoritas.m3u").exists()
    assert not (out / "Test Favorites").exists()


def test_add_name_is_kept_across_syncs(wired):
    searcher, _, out = wired
    save_config(Config(output_dir=str(out)))
    assert cli.main(["add", URL, "--name", "Mis favoritas"]) == 0
    assert load_config().playlists[0].name == "Mis favoritas"
    assert cli.main(["sync"]) == 0
    assert load_config().playlists[0].name == "Mis favoritas"
    assert (out / "Mis favoritas" / "Artist A - Song One.mp3").exists()
    assert not (out / "Test Favorites").exists()


def test_sync_does_not_overwrite_name_when_playlist_is_renamed(wired):
    _, _, out = wired
    save_config(Config(output_dir=str(out), playlists=[PlaylistEntry(URL, "Old name")]))
    assert cli.main(["sync"]) == 0
    assert load_config().playlists[0].name == "Old name"
    assert (out / "Old name").is_dir() and not (out / "Test Favorites").exists()


def test_sync_reports_progress_per_track(wired, tmp_path):
    searcher, downloader, out = wired
    cfg = Config(output_dir=str(out))
    session = cli.make_session()
    playlist = cli.read_playlist(URL, cfg, session)
    events: list[tuple[int, str, str]] = []
    sync_playlist(
        playlist,
        cfg,
        State.load(tmp_path / "s.json"),
        searcher,
        downloader,
        session,
        out,
        log=lambda _: None,
        on_progress=lambda i, kind, status: events.append((i, kind, status)),
    )
    assert [e[2] for e in events if e[0] == 0] == ["searching", "downloading", "done"]
    assert [e[2] for e in events if e[0] == 3] == ["searching", "not_found"]


def test_web_warns_when_listening_beyond_loopback(monkeypatch, capsys):
    from wantabag.web import server

    served = []
    monkeypatch.setattr(server, "serve", lambda *a: served.append(a) or 0)
    monkeypatch.setattr(cli, "check_ffmpeg", lambda: None)
    assert cli.main(["web", "--host", "0.0.0.0", "--port", "9999"]) == 0
    assert "no authentication" in capsys.readouterr().err
    assert served == [("0.0.0.0", 9999, False, False)]
    assert cli.main(["web"]) == 0
    assert capsys.readouterr().err == ""
