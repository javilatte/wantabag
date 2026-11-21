from wantabag.matching import AUDIO, VIDEO, best_match, norm, rank, score
from wantabag.models import Candidate, Track

TRACK = Track("Canción Uno (feat. Guest)", ("Artist A",), duration_ms=200_000)


def cand(title, channel="Artist A - Topic", dur=200, origin="youtube", vid="v"):
    return Candidate(vid, f"https://youtu.be/{vid}", title, channel, dur, origin)


def test_norm_strips_accents_case_and_punctuation():
    assert norm("  Canción—Uno!  ") == "cancion uno"


def test_official_topic_beats_live():
    topic = cand("Cancion Uno", vid="topic")
    live = cand("Artist A - Canción Uno (Live at Wembley)", channel="Artist A", vid="live")
    assert best_match(TRACK, [live, topic], AUDIO).video_id == "topic"


def test_cover_and_karaoke_are_rejected():
    cover = cand("Canción Uno (cover)", channel="Someone")
    karaoke = cand("Canción Uno karaoke version", channel="KaraokeHits")
    assert best_match(TRACK, [cover, karaoke], AUDIO) is None


def test_duration_off_by_40s_is_rejected():
    assert score(TRACK, cand("Canción Uno", dur=240), AUDIO) is None


def test_unknown_duration_is_neutral_not_rejected():
    assert score(TRACK, cand("Canción Uno", dur=None), AUDIO) is not None


def test_feat_and_accents_normalize():
    s = score(TRACK, cand("Cancion Uno", channel="Artist A"), AUDIO)
    assert s is not None and s >= AUDIO.threshold


def test_remix_accepted_when_original_is_a_remix():
    remix = Track("Song (Club Remix)", ("Artist A",), duration_ms=200_000)
    plain = Track("Song", ("Artist A",), duration_ms=200_000)
    c = cand("Song (Club Remix)")
    assert score(remix, c, AUDIO) > score(plain, c, AUDIO) + 0.25


def test_video_profile_prefers_official_video_and_allows_longer_gap():
    official = cand("Artist A - Canción Uno (Official Video)", channel="ArtistAVEVO", dur=225)
    assert best_match(TRACK, [official], VIDEO) is official
    assert best_match(TRACK, [official], AUDIO) is None


def test_rank_orders_by_score():
    good = cand("Canción Uno", vid="good", origin="ytmusic_song")
    meh = cand("Canción Uno lyrics", channel="Random", vid="meh")
    assert [c.video_id for _, c in rank(TRACK, [meh, good], AUDIO)][0] == "good"


def test_track_key_prefers_isrc():
    assert Track("A", ("B",), isrc="usx1").key() == "isrc:USX1"
    assert Track("Canción", ("B",)).key() == "t:b|cancion"
