"""Configuration: XDG paths, defaults, load/save and the single quality tables."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

AUDIO_QUALITIES: dict[str, tuple[str, str]] = {
    "v0": ("0", "MP3 VBR V0 (~245 kbps)"),
    "320": ("320", "MP3 320 kbps CBR"),
    "192": ("192", "MP3 192 kbps"),
    "128": ("128", "MP3 128 kbps"),
}
RECOMMENDED_AUDIO = "v0"
AUDIO_NOTE = (
    "Note: YouTube's audio source is usually ~128-160 kbps Opus/AAC and rarely above ~256 kbps;\n"
    "higher MP3 bitrates add file size, not detail."
)

VIDEO_QUALITIES: dict[str, int | None] = {"1080": 1080, "720": 720, "2160": 2160, "best": None}
RECOMMENDED_VIDEO = "1080"
VIDEO_LABELS = {"1080": "1080p", "720": "720p", "2160": "2160p (4K)", "best": "Best available"}

BACKENDS = ("youtube", "searxng")


class ConfigError(Exception):
    pass


def config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / "wantabag" / "config.toml"


def state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state"
    return Path(base) / "wantabag"


@dataclass
class PlaylistEntry:
    url: str
    name: str = ""


@dataclass
class Config:
    output_dir: str = "~/Music/wantabag"
    audio_quality: str = "v0"
    video_enabled: bool = False
    video_quality: str = RECOMMENDED_VIDEO
    search_backend: str = "youtube"
    searxng_url: str = ""
    spotify_client_id: str = ""
    spotify_client_secret: str = ""
    playlists: list[PlaylistEntry] = field(default_factory=list)

    @property
    def out_path(self) -> Path:
        return Path(self.output_dir).expanduser()


def _choice(value: object, allowed, where: str, path: Path) -> str:
    if value not in allowed:
        raise ConfigError(
            f"{path}: invalid value {value!r} for {where}; use one of {list(allowed)}"
        )
    return value  # type: ignore[return-value]


def load_config(path: Path | None = None) -> Config:
    path = path or config_path()
    cfg = Config()
    if not path.exists():
        return cfg
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: invalid TOML ({e})") from e

    cfg.output_dir = str(data.get("output_dir", cfg.output_dir))
    audio, video = data.get("audio", {}), data.get("video", {})
    search, spotify = data.get("search", {}), data.get("spotify", {})
    cfg.audio_quality = _choice(
        audio.get("quality", cfg.audio_quality), AUDIO_QUALITIES, "audio.quality", path
    )
    cfg.video_enabled = bool(video.get("enabled", cfg.video_enabled))
    cfg.video_quality = _choice(
        video.get("quality", cfg.video_quality), VIDEO_QUALITIES, "video.quality", path
    )
    cfg.search_backend = _choice(
        search.get("backend", cfg.search_backend), BACKENDS, "search.backend", path
    )
    cfg.searxng_url = str(search.get("searxng_url", ""))
    cfg.spotify_client_id = str(spotify.get("client_id", ""))
    cfg.spotify_client_secret = str(spotify.get("client_secret", ""))
    for item in data.get("playlists", []):
        if isinstance(item, dict) and item.get("url"):
            cfg.playlists.append(PlaylistEntry(str(item["url"]), str(item.get("name", ""))))
    return cfg


def _q(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    escaped = escaped.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    return f'"{escaped}"'


def dumps_config(cfg: Config) -> str:
    lines = [
        f"output_dir = {_q(cfg.output_dir)}",
        "",
        "[audio]",
        f"quality = {_q(cfg.audio_quality)}  # v0 | 320 | 192 | 128",
        "",
        "[video]",
        f"enabled = {str(cfg.video_enabled).lower()}",
        f"quality = {_q(cfg.video_quality)}  # 720 | 1080 | 2160 | best",
        "",
        "[search]",
        f"backend = {_q(cfg.search_backend)}  # youtube | searxng",
        f"searxng_url = {_q(cfg.searxng_url)}",
        "",
        "[spotify]  # optional, only needed for long Spotify playlists",
        f"client_id = {_q(cfg.spotify_client_id)}",
        f"client_secret = {_q(cfg.spotify_client_secret)}",
    ]
    for p in cfg.playlists:
        lines += ["", "[[playlists]]", f"url = {_q(p.url)}", f"name = {_q(p.name)}"]
    return "\n".join(lines) + "\n"


def save_config(cfg: Config, path: Path | None = None) -> Path:
    path = path or config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dumps_config(cfg), encoding="utf-8")
    path.chmod(0o600)
    return path
