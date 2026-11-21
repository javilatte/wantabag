import pytest

from wantabag.config import (
    Config,
    ConfigError,
    PlaylistEntry,
    config_path,
    load_config,
    save_config,
)
from wantabag.state import LockBusyError, State, run_lock


def test_defaults_without_file():
    cfg = load_config()
    assert cfg.audio_quality == "v0"
    assert cfg.video_enabled is False
    assert cfg.out_path.name == "wantabag"


def test_roundtrip_with_tricky_strings(tmp_path):
    cfg = Config(
        output_dir='C:\\Music\\"quoted"',
        audio_quality="320",
        video_enabled=True,
        video_quality="720",
        spotify_client_id="id",
        playlists=[PlaylistEntry("https://music.apple.com/x", 'Name "with" \\ quotes')],
    )
    path = save_config(cfg)
    assert path == config_path()
    assert path.stat().st_mode & 0o777 == 0o600
    assert load_config() == cfg


def test_invalid_value_mentions_file_and_key():
    path = config_path()
    path.parent.mkdir(parents=True)
    path.write_text('[audio]\nquality = "999"\n[future]\nunknown = 1\n')
    with pytest.raises(ConfigError, match=r"config\.toml.*audio\.quality"):
        load_config()


def test_state_persists_after_each_update(tmp_path):
    path = tmp_path / "s" / "state.json"
    state = State.load(path)
    state.update("k", status="done", video_id="v")
    again = State.load(path)
    assert again.get("k")["status"] == "done"
    assert not path.with_suffix(".tmp").exists()


def test_old_cached_apple_token_is_dropped(tmp_path):
    path = tmp_path / "state.json"
    path.write_text('{"apple_token": {"token": "t", "exp": 4102444800}, "tracks": {}}')
    state = State.load(path)
    state.update("k", status="done")
    assert "apple_token" not in path.read_text()


def test_lock_is_exclusive(tmp_path):
    with run_lock(tmp_path), pytest.raises(LockBusyError, match="in progress"), run_lock(tmp_path):
        pass
    with run_lock(tmp_path):
        pass
