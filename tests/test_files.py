from pathlib import Path

import pytest

from wantabag.files import (
    sanitize_filename,
    track_stems,
    write_m3u,
    write_missing_report,
)
from wantabag.models import Playlist, Track


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("AC/DC - Back In Black", "AC_DC - Back In Black"),
        ('What? "Yes": a|b<c>*', "What_ _Yes__ a_b_c__"),
        ("CON", "CON_"),
        ("nul.txt", "nul.txt_"),
        ("Trailing dots...  ", "Trailing dots"),
        ("   ", "Unknown"),
        ("", "Unknown"),
        ("Line\nbreak\ttab", "Line_break_tab"),
        ("Fiesta 🎉 Ñandú", "Fiesta 🎉 Ñandú"),
    ],
)
def test_sanitize(raw, expected):
    assert sanitize_filename(raw) == expected


def test_sanitize_truncates_on_character_boundary():
    out = sanitize_filename("é" * 200, max_bytes=181)
    assert len(out.encode()) <= 181
    assert set(out) == {"é"}


def test_sanitize_normalizes_to_nfc():
    assert sanitize_filename("e\u0301") == "\u00e9"


def _playlist(*titles):
    tracks = tuple(Track(t, ("A",), duration_ms=61000) for t in titles)
    return Playlist("apple", "pl.x", "u", "My List", tracks)


def test_repeated_names_get_suffix():
    assert track_stems(_playlist("Same", "Other", "Same")) == {
        0: "A - Same",
        1: "A - Other",
        2: "A - Same (2)",
    }


def test_m3u_lists_only_existing_files_in_order(tmp_path: Path):
    pl = _playlist("One", "Two", "Three")
    files = [(t, tmp_path / f"A - {t.title}.mp3") for t in pl.tracks]
    files[0][1].write_bytes(b"x")
    files[2][1].write_bytes(b"x")
    out = write_m3u(tmp_path, "My List", files)
    assert out.name == "My List.m3u"
    assert out.read_text(encoding="utf-8").splitlines() == [
        "#EXTM3U",
        "#EXTINF:61,A - One",
        "A - One.mp3",
        "#EXTINF:61,A - Three",
        "A - Three.mp3",
    ]


def test_missing_report_written_and_removed(tmp_path: Path):
    path = write_missing_report(tmp_path, ["A - X  [audio not found]"])
    assert path and "A - X" in path.read_text()
    assert write_missing_report(tmp_path, []) is None
    assert not path.exists()
