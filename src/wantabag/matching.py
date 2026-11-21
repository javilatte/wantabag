"""Pure functions that pick the best search candidate for a track."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from wantabag.models import Candidate, Track


def norm(text: str) -> str:
    """Casefold, strip accents and punctuation, collapse whitespace."""
    text = unicodedata.normalize("NFKD", text.casefold())
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


@dataclass(frozen=True)
class Profile:
    dur_abs_s: int
    dur_rel: float
    threshold: float
    bonus_words: tuple[str, ...]


AUDIO = Profile(dur_abs_s=15, dur_rel=0.10, threshold=0.55, bonus_words=("official audio",))
VIDEO = Profile(
    dur_abs_s=30,
    dur_rel=0.20,
    threshold=0.5,
    bonus_words=("official video", "official music video"),
)

_BAD_WORDS = (
    "live|en vivo|en directo|cover|karaoke|instrumental|remix|sped up|slowed|reverb|8d|"
    "nightcore|acoustic|reaction|tutorial|full album"
)
_BAD_RE = re.compile(rf"\b(?:{_BAD_WORDS})\b")
_LYRICS_RE = re.compile(r"\b(?:lyrics|letra)\b")
_NOISE_RE = re.compile(
    r"\((?:feat|ft|with)\b[^)]*\)|\[(?:feat|ft|with)\b[^\]]*\]"
    r"|\[(?:official|audio|video|lyric)[^\]]*\]|\((?:official|audio|video|lyric)[^)]*\)"
    r"|[-–]\s*(?:remaster(?:ed)?|\d{4} remaster)\b.*$",
    re.IGNORECASE,
)


def duration_tolerance(track_s: int, profile: Profile) -> float:
    return max(profile.dur_abs_s, track_s * profile.dur_rel)


def _clean_title(title: str) -> str:
    return norm(_NOISE_RE.sub(" ", title))


def _ratio(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio() if a and b else 0.0


def score(track: Track, cand: Candidate, profile: Profile) -> float | None:
    """Score a candidate in [0, ~1.2]; ``None`` means it must be rejected."""
    track_s = track.duration_s
    if track_s is not None and cand.duration_s is not None:
        diff = abs(track_s - cand.duration_s)
        tol = duration_tolerance(track_s, profile)
        if diff > tol:
            return None
        s_dur = 1 - diff / tol
    else:
        s_dur = 0.5

    channel = norm((cand.channel or "").removesuffix(" - Topic"))
    cand_title = norm(cand.title)
    haystack = f"{cand_title} {channel}"

    stripped = cand.title
    for artist in (*track.artists, track.artist_display):
        if artist:
            stripped = re.sub(re.escape(artist), " ", stripped, flags=re.IGNORECASE)
    s_title = _ratio(_clean_title(track.title), _clean_title(stripped))

    artists = [norm(a) for a in track.artists if a] or [norm(track.artist_display)]
    s_artist = sum(1 for a in artists if a and a in haystack) / len(artists)

    total = 0.35 * s_title + 0.25 * s_artist + 0.25 * s_dur

    if cand.origin == "ytmusic_song":
        total += 0.15
    if (cand.channel or "").endswith(" - Topic"):
        total += 0.10
    lowered = f"{cand.title} {cand.channel or ''}".casefold()
    if "vevo" in lowered or any(w in lowered for w in profile.bonus_words):
        total += 0.05

    original = track.title.casefold()
    bad_in_cand = set(_BAD_RE.findall(lowered))
    bad_in_orig = set(_BAD_RE.findall(original))
    total -= 0.3 * len(bad_in_cand - bad_in_orig)
    if _LYRICS_RE.search(lowered) and not _LYRICS_RE.search(original):
        total -= 0.05
    return total


def rank(track: Track, cands: list[Candidate], profile: Profile) -> list[tuple[float, Candidate]]:
    """Candidates above the profile threshold, best first."""
    scored = [(s, c) for c in cands if (s := score(track, c, profile)) is not None]
    return sorted((sc for sc in scored if sc[0] >= profile.threshold), key=lambda sc: -sc[0])


def best_match(track: Track, cands: list[Candidate], profile: Profile) -> Candidate | None:
    ranked = rank(track, cands, profile)
    return ranked[0][1] if ranked else None
