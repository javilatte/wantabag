"""The web interface against a real server on a free loopback port; downloads are simulated."""

import http.client
import io
import json
import re
import threading
import zipfile
from pathlib import Path

import pytest

from tests.fakes import FakeDownloader, FakeResponse, FakeSearcher, FakeSession, fixture_text
from wantabag.config import AUDIO_QUALITIES
from wantabag.web import jobs, server

URL = "https://music.apple.com/es/playlist/test-favorites/pl.u-TEST"
JSON = {"Content-Type": "application/json"}


class Client:
    def __init__(self, port: int) -> None:
        self.port = port

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        data = json.dumps(body).encode() if body is not None else None
        hdrs = dict(JSON if body is not None else {}, **(headers or {}))
        conn.request(method, path, data, hdrs)
        resp = conn.getresponse()
        return resp, resp.read()

    def json(self, method, path, body=None, headers=None):
        resp, raw = self.request(method, path, body, headers)
        return resp.status, json.loads(raw)

    def events(self, path, headers=None) -> list[tuple[int, str, dict]]:
        resp, raw = self.request("GET", path, headers=headers)
        assert resp.status == 200
        assert resp.getheader("Content-Type").startswith("text/event-stream")
        out = []
        for block in raw.decode().split("\n\n"):
            if "event:" not in block:
                continue
            fields = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
            out.append((int(fields["id"]), fields["event"], json.loads(fields["data"])))
        return out

    def wait_status(self, path, status):
        for _ in range(200):
            code, snap = self.json("GET", path)
            if snap.get("status") == status:
                return snap
            threading.Event().wait(0.02)
        raise AssertionError(f"never reached {status}: {snap}")


@pytest.fixture
def web(monkeypatch, tmp_path):
    session = FakeSession({URL: FakeResponse(fixture_text("apple_playlist.html"))})
    state = {"downloader": FakeDownloader(), "max_queued": 5}
    monkeypatch.setattr(jobs, "make_session", lambda: session)
    monkeypatch.setattr(jobs, "make_searcher", lambda *a, **k: FakeSearcher(missing={"Fourth"}))
    monkeypatch.setattr(jobs, "Downloader", lambda *a, **k: state["downloader"])
    monkeypatch.setattr(server, "check_ffmpeg", lambda: None)
    started = []

    def start(**kwargs):
        manager = jobs.JobManager(tmp_path / "jobs", **kwargs)
        srv = server.make_server("127.0.0.1", 0, manager)
        threading.Thread(target=srv.serve_forever, args=(0.02,), daemon=True).start()
        started.append((srv, manager))
        return Client(srv.server_address[1]), manager, tmp_path / "jobs"

    yield start, state
    for srv, manager in started:
        srv.shutdown()
        srv.server_close()
        manager.close()


@pytest.fixture
def client(web):
    return web[0]()


def submit(c: Client, **extra) -> str:
    status, body = c.json("POST", "/api/jobs", {"url": URL, **extra})
    assert status == 202, body
    return body["id"]


def test_job_streams_events_and_zip_then_cleans_up(client):
    c, manager, base = client
    job_id = submit(c)
    c.wait_status(f"/api/jobs/{job_id}", "done")
    events = c.events(f"/api/jobs/{job_id}/events")
    kinds = [kind for _, kind, _ in events]
    assert kinds[0] == "queued" and kinds[1] == "playlist" and kinds[-1] == "end"
    assert [i for i, _, _ in events] == list(range(len(events)))
    playlist = events[1][2]
    assert (playlist["name"], playlist["count"]) == ("Test Favorites", 5)
    first = [d["status"] for _, k, d in events if k == "track" and d["index"] == 0]
    assert first == ["searching", "downloading", "done"]
    fourth = [d["status"] for _, k, d in events if k == "track" and d["index"] == 3]
    assert fourth[-1] == "not_found"
    end = events[-1][2]
    assert end["status"] == "done" and end["downloaded"] == 4
    assert [(m["index"], m["reason"]) for m in end["missing"]] == [(3, "not_found")]

    resp, raw = c.request("GET", end["zip_url"])
    assert resp.status == 200 and resp.getheader("Content-Type") == "application/zip"
    assert 'filename="Test Favorites.zip"' in resp.getheader("Content-Disposition")
    names = sorted(zipfile.ZipFile(io.BytesIO(raw)).namelist())
    assert len([n for n in names if n.endswith(".mp3")]) == 4
    assert "Test Favorites/Test Favorites.m3u" in names
    assert "Test Favorites/wantabag-missing.txt" in names
    assert all(n.startswith("Test Favorites/") for n in names)

    for _ in range(100):
        if not (base / job_id).exists():
            break
        threading.Event().wait(0.02)
    assert c.json("GET", f"/api/jobs/{job_id}")[0] == 404
    assert not (base / job_id).exists()


def test_name_and_quality_options_reach_the_job(client):
    c, manager, base = client
    job_id = submit(c, name="Mis favoritas", quality="320", videos="720")
    c.wait_status(f"/api/jobs/{job_id}", "done")
    job = manager.get(job_id)
    assert (job.cfg.audio_quality, job.cfg.video_enabled, job.cfg.video_quality) == (
        "320",
        True,
        "720",
    )
    resp, raw = c.request("GET", f"/api/jobs/{job_id}/zip")
    names = zipfile.ZipFile(io.BytesIO(raw)).namelist()
    assert "Mis favoritas/Mis favoritas.m3u" in names
    assert any(n.endswith("Song One.mp4") for n in names)
    assert 'filename="Mis favoritas.zip"' in resp.getheader("Content-Disposition")


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/playlist/abc",
        "spotify:playlist:abc123",
        "ftp://music.apple.com/es/playlist/x/pl.u-1",
        "http://music.apple.com/es/playlist/x/pl.u-1",
        "",
        None,
    ],
)
def test_invalid_url_rejected(client, url):
    c, *_ = client
    for path in ("/api/jobs", "/api/preview"):
        status, body = c.json("POST", path, {"url": url})
        assert status == 400 and "Only Apple Music and Spotify" in body["error"]


@pytest.mark.parametrize(
    "extra",
    [{"quality": "9999"}, {"videos": "4k"}, {"name": 5}, {"name": "x" * 500}],
)
def test_invalid_options_rejected(client, extra):
    c, *_ = client
    assert c.json("POST", "/api/jobs", {"url": URL, **extra})[0] == 400


def test_post_requires_json_content_type(client):
    c, *_ = client
    resp, _ = c.request(
        "POST", "/api/jobs", None, {"Content-Type": "text/plain", "Content-Length": "0"}
    )
    assert resp.status == 415
    assert c.request("POST", "/api/jobs", None)[0].status == 415


def test_malformed_and_oversized_bodies(client):
    c, *_ = client
    conn = http.client.HTTPConnection("127.0.0.1", c.port, timeout=5)
    conn.request("POST", "/api/jobs", b"[1]", JSON)
    assert conn.getresponse().status == 400
    conn = http.client.HTTPConnection("127.0.0.1", c.port, timeout=5)
    conn.request("POST", "/api/jobs", b"{" + b" " * 20000 + b"}", JSON)
    assert conn.getresponse().status == 413


def test_foreign_host_header_forbidden(client):
    c, *_ = client
    assert c.json("GET", "/api/options", headers={"Host": "evil.example"})[0] == 403
    assert c.request("GET", "/", headers={"Host": "evil.example"})[0].status == 403
    assert c.json("GET", "/api/options", headers={"Host": f"localhost:{c.port}"})[0] == 200


def test_unknown_routes_and_methods(client):
    c, *_ = client
    assert c.json("GET", "/api/nope")[0] == 404
    assert c.json("DELETE", "/api/jobs/abc")[0] == 405
    assert c.json("GET", "/api/jobs")[0] == 405
    assert c.json("GET", "/api/jobs/does-not-exist")[0] == 404


def test_preview_lists_tracks(client):
    c, *_ = client
    status, body = c.json("POST", "/api/preview", {"url": URL})
    assert status == 200
    assert (body["name"], body["count"], body["source"]) == ("Test Favorites", 5, "apple")
    assert body["total_duration_s"] == sum(t["duration_s"] or 0 for t in body["tracks"]) > 0
    assert body["tracks"][0]["label"] == "Artist A - Song One"


def test_events_resume_after_last_event_id(client):
    c, *_ = client
    job_id = submit(c)
    c.wait_status(f"/api/jobs/{job_id}", "done")
    everything = c.events(f"/api/jobs/{job_id}/events")
    tail = c.events(f"/api/jobs/{job_id}/events", {"Last-Event-ID": "3"})
    assert tail == everything[4:]


def test_queue_runs_one_at_a_time_and_caps(web):
    start, state = web
    release = threading.Event()
    inner = state["downloader"]

    class Blocking:
        def audio(self, *a):
            release.wait(5)
            return inner.audio(*a)

        video = audio

    state["downloader"] = Blocking()
    c, manager, _ = start(max_queued=1)
    try:
        first = submit(c)
        c.wait_status(f"/api/jobs/{first}", "downloading")
        second = submit(c)
        snap = c.json("GET", f"/api/jobs/{second}")[1]
        assert snap["status"] == "queued" and snap["position"] == 1
        status, body = c.json("POST", "/api/jobs", {"url": URL})
        assert status == 429 and "waiting" in body["error"]
    finally:
        release.set()
    c.wait_status(f"/api/jobs/{first}", "done")
    c.wait_status(f"/api/jobs/{second}", "done")


def test_finished_jobs_expire_after_ttl(web):
    start, state = web
    now = [100.0]
    c, manager, base = start(ttl=60, clock=lambda: now[0])
    job_id = submit(c)
    c.wait_status(f"/api/jobs/{job_id}", "done")
    manager.reap(now[0] + 30)
    assert manager.get(job_id) is not None
    manager.reap(now[0] + 61)
    assert manager.get(job_id) is None and not (base / job_id).exists()


def test_running_jobs_are_never_reaped(web):
    start, state = web
    release = threading.Event()
    inner = state["downloader"]

    class Blocking:
        def audio(self, *a):
            release.wait(5)
            return inner.audio(*a)

    state["downloader"] = Blocking()
    c, manager, _ = start(ttl=0)
    try:
        job_id = submit(c)
        c.wait_status(f"/api/jobs/{job_id}", "downloading")
        manager.reap(10**9)
        assert manager.get(job_id) is not None
    finally:
        release.set()
    c.wait_status(f"/api/jobs/{job_id}", "done")


def test_unreadable_playlist_ends_with_an_error(client, monkeypatch):
    c, *_ = client
    monkeypatch.setattr(jobs, "make_session", lambda: FakeSession({}))
    job_id = submit(c)
    snap = c.wait_status(f"/api/jobs/{job_id}", "error")
    assert snap["error"] and snap["zip_url"] is None
    end = c.events(f"/api/jobs/{job_id}/events")[-1]
    assert end[1] == "end" and end[2]["status"] == "error"
    assert c.request("GET", f"/api/jobs/{job_id}/zip")[0].status == 409


def test_missing_ffmpeg_returns_503(client, monkeypatch):
    c, *_ = client
    monkeypatch.setattr(server, "check_ffmpeg", lambda: "ffmpeg is required but was not found.")
    status, body = c.json("POST", "/api/jobs", {"url": URL})
    assert status == 503 and "ffmpeg is required" in body["error"]


def test_zip_not_ready_is_409(web):
    start, state = web
    release = threading.Event()
    inner = state["downloader"]

    class Blocking:
        def audio(self, *a):
            release.wait(5)
            return inner.audio(*a)

    state["downloader"] = Blocking()
    c, *_ = start()
    try:
        job_id = submit(c)
        assert c.json("GET", f"/api/jobs/{job_id}/zip")[0] == 409
    finally:
        release.set()
    c.wait_status(f"/api/jobs/{job_id}", "done")


def test_options_lists_setup_qualities(client):
    c, *_ = client
    status, body = c.json("GET", "/api/options")
    assert status == 200
    assert [o["key"] for o in body["audio"]] == list(AUDIO_QUALITIES)
    assert [o["key"] for o in body["audio"] if o["recommended"]] == ["v0"]
    assert [o["key"] for o in body["video"] if o["recommended"]] == ["1080"]
    assert body["defaults"] == {"quality": "v0", "videos": False}
    assert "rarely above" in body["audio_note"]


def test_static_assets_are_local(client):
    c, *_ = client
    resp, html = c.request("GET", "/")
    assert resp.status == 200 and b"<title>" in html
    assert "default-src 'self'" in resp.getheader("Content-Security-Policy")
    assert resp.getheader("X-Content-Type-Options") == "nosniff"
    font, data = c.request("GET", "/fonts/AlloVariable.woff2")
    assert font.status == 200 and font.getheader("Content-Type") == "font/woff2"
    assert data[:4] == b"wOF2"
    assert c.request("GET", "/../pyproject.toml")[0].status == 404
    assert c.request("GET", "/%2e%2e/pyproject.toml")[0].status == 404
    assert c.request("HEAD", "/")[0].status == 200

    external = re.compile(r"""(?:src|href)\s*=\s*["']https?://|url\(\s*["']?https?://""")
    for name in ("index.html", "app.css", "app.js"):
        text = server.load_static()[f"/{name}"][0].decode()
        assert not external.search(text), name


def test_static_files_are_packaged_under_the_package():
    static = Path(server.__file__).parent / "static"
    assert {"index.html", "app.css", "app.js", "favicon.svg"} <= {p.name for p in static.iterdir()}


def test_is_loopback():
    assert server.is_loopback("127.0.0.1") and server.is_loopback("localhost")
    assert server.is_loopback("::1")
    assert not server.is_loopback("0.0.0.0") and not server.is_loopback("192.168.1.5")
    assert not server.is_loopback("my-host.local")


def test_content_disposition_keeps_non_ascii_names():
    header = server.content_disposition("Canción favorita.zip")
    assert 'filename="Cancion favorita.zip"' in header
    assert "filename*=UTF-8''Canci%C3%B3n%20favorita.zip" in header
