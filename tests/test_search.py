from tests.fakes import FakeResponse, FakeSession, fixture_text
from wantabag.models import Track
from wantabag.search import SearxngSearcher, YtDlpSearcher, entry_to_candidate, parse_length

TRACK = Track("Song One", ("Artist A",), duration_ms=200_000, isrc="USAAA0000001")


class FakeYDL:
    queries: list[str] = []

    def __init__(self, entries_by_query):
        self.entries_by_query = entries_by_query

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, query, download=False):
        FakeYDL.queries.append(query)
        return {"entries": self.entries_by_query.get(query, [])}


def factory_for(entries_by_query):
    FakeYDL.queries = []
    return lambda opts: FakeYDL(entries_by_query)


def flat(vid, title, channel, duration):
    return {
        "id": vid,
        "title": title,
        "channel": channel,
        "duration": duration,
        "thumbnails": [{"url": "http://t/small"}, {"url": "http://t/big"}],
    }


def test_entry_conversion():
    c = entry_to_candidate(flat("abc", "T", "Ch", 200.0), "youtube")
    assert (c.video_id, c.url, c.duration_s, c.thumbnail) == (
        "abc",
        "https://www.youtube.com/watch?v=abc",
        200,
        "http://t/big",
    )
    assert entry_to_candidate({"id": "x"}, "youtube") is None


def test_stops_after_isrc_query_when_match_is_great():
    isrc_q = "https://music.youtube.com/search?q=USAAA0000001#songs"
    good = flat("good", "Artist A - Song One", "Artist A - Topic", 200)
    searcher = YtDlpSearcher(ydl_factory=factory_for({isrc_q: [good]}))
    cands = searcher.search(TRACK)
    assert [c.video_id for c in cands] == ["good"]
    assert cands[0].origin == "ytmusic_song"
    assert FakeYDL.queries == [isrc_q]


def test_falls_through_queries_when_nothing_good():
    searcher = YtDlpSearcher(ydl_factory=factory_for({}))
    assert searcher.search(TRACK) == []
    assert FakeYDL.queries[-1] == "ytsearch5:Artist A - Song One"
    assert len(FakeYDL.queries) == 3


def test_video_search_query():
    searcher = YtDlpSearcher(ydl_factory=factory_for({}))
    searcher.search(TRACK, "video")
    assert FakeYDL.queries == ["ytsearch5:Artist A - Song One official video"]


def test_parse_length():
    assert parse_length("3:20") == 200
    assert parse_length("1:03:20") == 3800
    assert parse_length(90) == 90
    assert parse_length("n/a") is None


def test_searxng_results_filtered_and_converted():
    url = "http://sx"
    session = FakeSession({f"{url}/search": FakeResponse(fixture_text("searxng_results.json"))})
    searcher = SearxngSearcher(url, session, YtDlpSearcher(ydl_factory=factory_for({})))
    cands = searcher.search(TRACK)
    assert [(c.video_id, c.duration_s, c.origin) for c in cands] == [
        ("abc12345678", 200, "searxng"),
        ("xyz98765432", 3800, "searxng"),
    ]
    assert session.calls[0][1]["params"]["format"] == "json"


def test_searxng_error_falls_back_to_ytdlp(capsys):
    session = FakeSession({})
    searcher = SearxngSearcher("http://sx", session, YtDlpSearcher(ydl_factory=factory_for({})))
    searcher.search(TRACK)
    searcher.search(TRACK)
    assert FakeYDL.queries
    assert capsys.readouterr().err.count("SearXNG unavailable") == 1
