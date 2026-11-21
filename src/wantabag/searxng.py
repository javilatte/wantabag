"""Optional SearXNG search backend, run locally with docker."""

from __future__ import annotations

import contextlib
import secrets
import subprocess
import time
from collections.abc import Callable

import requests

from wantabag.config import Config, config_path, save_config

CONTAINER = "wantabag-searxng"
IMAGE = "searxng/searxng:latest"
PORT = 8888
URL = f"http://127.0.0.1:{PORT}"
NO_DOCKER_HELP = """docker is not available, so SearXNG cannot be started here.
Install docker (https://docs.docker.com/engine/install/) and make sure 'docker info' works,
or point wantabag at an existing SearXNG instance by setting in {config}:
  [search]
  backend = "searxng"
  searxng_url = "http://your-host:8080"
(the instance must allow the JSON format: search.formats: [html, json])."""

Run = Callable[..., subprocess.CompletedProcess]


class SearxngError(Exception):
    pass


def settings_yaml(secret: str) -> str:
    return (
        "use_default_settings: true\n"
        "server:\n"
        f'  secret_key: "{secret}"\n'
        "  limiter: false\n"
        "search:\n"
        "  formats:\n"
        "    - html\n"
        "    - json\n"
    )


def _docker(run: Run, args: list[str]) -> subprocess.CompletedProcess:
    return run(["docker", *args], capture_output=True, text=True)


def _wait_ready(session: requests.Session, timeout: float, sleep: Callable[[float], None]) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if session.get(f"{URL}/search", params={"q": "test", "format": "json"}).ok:
                return True
        except requests.RequestException:
            pass
        sleep(1)
    return False


def up(
    cfg: Config,
    session: requests.Session,
    run: Run = subprocess.run,
    sleep: Callable[[float], None] = time.sleep,
    timeout: float = 30,
) -> str:
    try:
        info = _docker(run, ["info"])
    except FileNotFoundError:
        raise SearxngError(NO_DOCKER_HELP.format(config=config_path())) from None
    if info.returncode != 0:
        raise SearxngError(NO_DOCKER_HELP.format(config=config_path()))

    exists = _docker(run, ["ps", "-a", "--filter", f"name=^{CONTAINER}$", "--format", "{{.Names}}"])
    if CONTAINER in exists.stdout.split():
        proc = _docker(run, ["start", CONTAINER])
    else:
        conf_dir = config_path().parent / "searxng"
        conf_dir.mkdir(parents=True, exist_ok=True)
        settings = conf_dir / "settings.yml"
        if not settings.exists():
            settings.write_text(settings_yaml(secrets.token_hex(32)), encoding="utf-8")
        proc = _docker(
            run,
            [
                "run",
                "-d",
                "--name",
                CONTAINER,
                "--restart",
                "unless-stopped",
                "-p",
                f"127.0.0.1:{PORT}:8080",
                "-v",
                f"{conf_dir}:/etc/searxng",
                IMAGE,
            ],
        )
    if proc.returncode != 0:
        raise SearxngError(f"docker failed: {proc.stderr.strip()}")

    cfg.search_backend, cfg.searxng_url = "searxng", URL
    save_config(cfg)
    if not _wait_ready(session, timeout, sleep):
        return f"SearXNG started but is not answering yet at {URL}; give it a minute."
    return f"SearXNG is running at {URL} and set as the search backend."


def down(cfg: Config, run: Run = subprocess.run) -> str:
    with contextlib.suppress(FileNotFoundError):
        _docker(run, ["stop", CONTAINER])
    cfg.search_backend = "youtube"
    save_config(cfg)
    return "SearXNG stopped; search backend set back to youtube."
