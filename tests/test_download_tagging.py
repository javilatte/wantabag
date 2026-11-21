import shutil
from pathlib import Path

import pytest
from mutagen.id3 import ID3, TXXX
from mutagen.mp3 import MP3

from tests.fakes import FIXTURES
from wantabag import download
from wantabag.download import Downloader, DownloadError, DurationMismatch, check_ffmpeg
from wantabag.models import Candidate, Track
from wantabag.tagging import write_tags

TRACK = Track(
    "Song One",
    ("Artist A",),
    "Album One",
    200_000,
    "USAAA0000001",
    "http://art",
    artist_display="Artist A",
)
CAND = Candidate("vid", "https://www.youtube.com/watch?v=vid", "t", "c", 200, "youtube")


def test_write_tags_sets_all_frames(tmp_path: Path):
    mp3 = tmp_path / "a.mp3"
    shutil.copy(FIXTURES / "silence.mp3", mp3)
    stale = ID3()
    stale.add(TXXX(encoding=3, desc="major_brand", text="M4A "))
    stale.save(mp3)
    write_tags(mp3, TRACK, 2, 240, "Album One", (b"\xff\xd8jpegdata", "image/jpeg"), "https://src")
    tags = ID3(mp3)
    assert tags["TIT2"].text == ["Song One"]
    assert tags["TPE1"].text == ["Artist A"]
    assert tags["TALB"].text == ["Album One"]
    assert tags["TRCK"].text == ["2/240"]
    assert tags["TSRC"].text == ["USAAA0000001"]
    assert tags["APIC:Cover"].data == b"\xff\xd8jpegdata"
    assert tags["APIC:Cover"].type == 3
    assert tags["TXXX:wantabag_source"].text == ["https://src"]
    assert "TXXX:major_brand" not in tags
    assert tags.version[:2] == (2, 3)
    assert MP3(mp3).info.length > 0


def test_check_ffmpeg(monkeypatch):
    monkeypatch.setattr(download.shutil, "which", lambda name: "/usr/bin/" + name)
    assert check_ffmpeg() is None
    monkeypatch.setattr(download.shutil, "which", lambda name: None if name == "ffprobe" else "/x")
    hint = check_ffmpeg()
    assert "apt install ffmpeg" in hint and "brew install ffmpeg" in hint


class FakeYDL:
    def __init__(self, opts, duration=200):
        self.opts, self.duration = opts, duration

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download=False):
        return {"duration": self.duration, "url": url}

    def process_ie_result(self, info, download=True):
        base = self.opts["outtmpl"].replace("%(ext)s", "mp3")
        Path(base).write_bytes(b"audio")
        Path(base.replace("mp3", "part")).write_bytes(b"junk")
        return {**info, "album": "A"}


def test_audio_download_renames_and_cleans_temp(tmp_path: Path):
    dest = tmp_path / "pl" / "Artist [A] - Song*.mp3"
    dl = Downloader("v0", "1080", ydl_factory=lambda o: FakeYDL(o))
    info = dl.audio(CAND, TRACK, dest)
    assert dest.read_bytes() == b"audio"
    assert info["album"] == "A"
    assert [p.name for p in dest.parent.iterdir()] == [dest.name]


def test_duration_mismatch_downloads_nothing(tmp_path: Path):
    dest = tmp_path / "x.mp3"
    dl = Downloader("v0", "1080", ydl_factory=lambda o: FakeYDL(o, duration=400))
    with pytest.raises(DurationMismatch):
        dl.audio(CAND, TRACK, dest)
    assert not dest.exists()


def test_yt_dlp_errors_become_download_errors(tmp_path: Path):
    class Boom(FakeYDL):
        def extract_info(self, url, download=False):
            raise RuntimeError("unavailable")

    with pytest.raises(DownloadError, match="unavailable"):
        Downloader("v0", "1080", ydl_factory=lambda o: Boom(o)).audio(
            CAND, TRACK, tmp_path / "x.mp3"
        )


def test_format_options():
    assert download.audio_format_opts("v0")["postprocessors"][0]["preferredquality"] == "0"
    assert download.audio_format_opts("320")["postprocessors"][0]["preferredquality"] == "320"
    assert "height<=720" in download.video_format("720")
    assert download.video_format("best") == "bv*+ba/b"


def test_ytdlp_output_is_silenced_unless_verbose(capfd):
    logger = download.base_opts(False)["logger"]
    for method in (logger.debug, logger.info, logger.warning, logger.error):
        method("ERROR: unable to download video data: HTTP Error 403: Forbidden")
    captured = capfd.readouterr()
    assert captured.out == "" and captured.err == ""
    assert "logger" not in download.base_opts(True)
