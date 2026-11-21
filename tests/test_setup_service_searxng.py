import plistlib
import subprocess
from pathlib import Path

import pytest

from tests.fakes import FakeResponse, FakeSession
from wantabag import searxng, service
from wantabag.config import Config
from wantabag.setup_wizard import run_setup


def scripted(answers):
    it = iter(answers)
    shown: list[str] = []

    def input_fn(prompt):
        shown.append(prompt)
        return next(it)

    return input_fn, shown


def test_setup_accepts_defaults_with_enter():
    input_fn, _ = scripted(["", "", "", "", ""])
    out: list[str] = []
    cfg, install, install_web = run_setup(Config(), input_fn, out.append)
    assert (cfg.output_dir, cfg.audio_quality, cfg.video_enabled) == (
        "~/Music/wantabag",
        "v0",
        False,
    )
    assert install is False and install_web is False
    text = "\n".join(out)
    assert "VBR V0 (~245 kbps) (recommended)" in text or "(~245 kbps)" in text
    assert "rarely above ~256 kbps" in text


def test_setup_custom_choices_and_playlists():
    answers = [
        "/music",
        "2",
        "y",
        "2",
        "https://example.com/nope",
        "https://open.spotify.com/playlist/AbC123",
        "https://music.apple.com/es/playlist/x/pl.u-1",
        "",
        "my-id",
        "my-secret",
        "",
        "y",
    ]
    input_fn, _ = scripted(answers)
    out: list[str] = []
    cfg, install, install_web = run_setup(Config(), input_fn, out.append)
    assert cfg.output_dir == "/music"
    assert cfg.audio_quality == "320"
    assert cfg.video_enabled and cfg.video_quality == "720"
    assert [p.url for p in cfg.playlists] == [
        "https://open.spotify.com/playlist/AbC123",
        "https://music.apple.com/es/playlist/x/pl.u-1",
    ]
    assert (cfg.spotify_client_id, cfg.spotify_client_secret) == ("my-id", "my-secret")
    assert install is True and install_web is True
    assert any("Only Apple Music and Spotify" in line for line in out)
    assert any("1080p (recommended)" in line for line in out)


def test_setup_asks_folder_name_proposing_the_playlist_name():
    names = {"https://music.apple.com/es/playlist/x/pl.u-1": "Favorite Songs"}
    answers = ["", "", "", "https://music.apple.com/es/playlist/x/pl.u-1", "Canciones", "", "", ""]
    input_fn, shown = scripted(answers)
    cfg, _, _ = run_setup(Config(), input_fn, lambda _: None, read_name=names.__getitem__)
    assert cfg.playlists[0].name == "Canciones"
    assert any("Folder name [Favorite Songs]" in p for p in shown)


def test_setup_unreadable_playlist_leaves_name_empty_and_warns():
    def fail(url):
        raise ValueError("boom")

    answers = ["", "", "", "https://music.apple.com/es/playlist/x/pl.u-1", "", "", ""]
    input_fn, _ = scripted(answers)
    out: list[str] = []
    cfg, _, _ = run_setup(Config(), input_fn, out.append, read_name=fail)
    assert cfg.playlists[0].name == ""
    assert any("Could not read the playlist" in line for line in out)


class Recorder:
    def __init__(self, fail_on: str | None = None):
        self.calls: list[list[str]] = []
        self.fail_on = fail_on

    def __call__(self, args, **kwargs):
        self.calls.append(args)
        code = 1 if self.fail_on and self.fail_on in args else 0
        return subprocess.CompletedProcess(args, code, stdout="out", stderr="err")


def test_unit_and_timer_content():
    unit = service.render_unit(["/opt/my venv/bin/python", "-m", "wantabag", "sync"])
    assert 'ExecStart="/opt/my venv/bin/python" "-m" "wantabag" "sync"' in unit
    assert "Type=oneshot" in unit
    timer = service.render_timer()
    for line in (
        "OnCalendar=daily",
        "Persistent=true",
        "RandomizedDelaySec=30min",
        "WantedBy=timers.target",
    ):
        assert line in timer


def test_plist_content():
    data = plistlib.loads(service.render_plist(["/py", "-m", "wantabag", "sync"], Path("/log")))
    assert data["Label"] == "io.github.wantabag.sync"
    assert data["StartCalendarInterval"]["Hour"] == 10
    assert data["RunAtLoad"] is False
    assert "/opt/homebrew/bin" in data["EnvironmentVariables"]["PATH"]
    assert data["StandardOutPath"] == "/log"


def test_web_unit_is_a_long_running_service():
    cmd = ["/py", "-m", "wantabag", "web", "--host", "127.0.0.1", "--port", "8765"]
    unit = service.render_unit(cmd, "wantabag: web interface", daily=False)
    for line in ("Type=simple", "Restart=on-failure", "WantedBy=default.target"):
        assert line in unit
    assert '"web" "--host"' in unit and "Type=oneshot" not in unit


def test_web_plist_stays_alive_without_a_calendar():
    plist = service.render_plist(["/py"], Path("/log"), "io.github.wantabag.web", daily=False)
    data = plistlib.loads(plist)
    assert data["Label"] == "io.github.wantabag.web"
    assert data["KeepAlive"] is True and data["RunAtLoad"] is True
    assert "StartCalendarInterval" not in data


def test_web_service_install_uninstall_linux(tmp_path: Path):
    run = Recorder()
    svc = service.Service(run, home=tmp_path, system="Linux", spec=service.web_spec("127.0.0.1", 9))
    assert "Not installed. Run: wantabag service install --web" in svc.status()
    svc.install()
    unit_dir = tmp_path / "config" / "systemd" / "user"
    unit = (unit_dir / "wantabag-web.service").read_text()
    assert '"--port" "9"' in unit
    assert not (unit_dir / "wantabag-web.timer").exists()
    assert ["systemctl", "--user", "enable", "--now", "wantabag-web.service"] in run.calls
    svc.uninstall()
    assert not (unit_dir / "wantabag-web.service").exists()


def test_web_service_install_macos(tmp_path: Path):
    run = Recorder()
    svc = service.Service(run, home=tmp_path, system="Darwin", uid=501, spec=service.web_spec())
    svc.install()
    plist = tmp_path / "Library/LaunchAgents/io.github.wantabag.web.plist"
    assert plist.exists()
    assert ["launchctl", "bootstrap", "gui/501", str(plist)] in run.calls


def test_linux_install_uninstall(tmp_path: Path):
    run = Recorder()
    svc = service.Service(run, home=tmp_path, system="Linux")
    svc.install()
    unit_dir = tmp_path / "config" / "systemd" / "user"
    assert (unit_dir / "wantabag-sync.service").exists()
    assert (unit_dir / "wantabag-sync.timer").exists()
    assert ["systemctl", "--user", "daemon-reload"] in run.calls
    assert ["systemctl", "--user", "enable", "--now", "wantabag-sync.timer"] in run.calls
    svc.status()
    assert ["systemctl", "--user", "list-timers", "wantabag-sync.timer", "--no-pager"] in run.calls
    svc.uninstall()
    assert not (unit_dir / "wantabag-sync.timer").exists()
    assert ["systemctl", "--user", "disable", "--now", "wantabag-sync.timer"] in run.calls
    assert "Not installed" in svc.status()


def test_macos_install_uninstall(tmp_path: Path):
    run = Recorder()
    svc = service.Service(run, home=tmp_path, system="Darwin", uid=501)
    svc.install()
    plist = tmp_path / "Library/LaunchAgents/io.github.wantabag.sync.plist"
    assert plist.exists()
    assert ["launchctl", "bootstrap", "gui/501", str(plist)] in run.calls
    svc.uninstall()
    assert not plist.exists()
    assert ["launchctl", "bootout", "gui/501", str(plist)] in run.calls


def test_install_failure_is_reported(tmp_path: Path):
    svc = service.Service(Recorder(fail_on="daemon-reload"), home=tmp_path, system="Linux")
    with pytest.raises(service.ServiceError, match="reload systemd"):
        svc.install()


def test_unsupported_system():
    with pytest.raises(service.ServiceError, match="only supported on Linux and macOS"):
        service.Service(Recorder(), system="Windows")


def test_searxng_up_without_docker_explains(monkeypatch):
    def no_docker(args, **kw):
        raise FileNotFoundError("docker")

    with pytest.raises(searxng.SearxngError) as e:
        searxng.up(Config(), FakeSession({}), run=no_docker)
    msg = str(e.value)
    assert "docker" in msg and "searxng_url" in msg and "config.toml" in msg


def test_searxng_up_starts_container_and_updates_config():
    run = Recorder()
    session = FakeSession({"http://127.0.0.1:8888/search": FakeResponse("{}")})
    cfg = Config()
    msg = searxng.up(cfg, session, run=run, sleep=lambda s: None)
    assert "running" in msg
    run_cmd = next(c for c in run.calls if c[1] == "run")
    assert "127.0.0.1:8888:8080" in run_cmd and "wantabag-searxng" in run_cmd
    assert (cfg.search_backend, cfg.searxng_url) == ("searxng", "http://127.0.0.1:8888")
    settings = (Path(searxng.config_path()).parent / "searxng" / "settings.yml").read_text()
    assert "- json" in settings and "limiter: false" in settings


def test_searxng_down_restores_backend():
    cfg = Config(search_backend="searxng")
    searxng.down(cfg, run=Recorder())
    assert cfg.search_backend == "youtube"
