"""Interactive `wantabag setup`. Input and output are injected so it is testable."""

from __future__ import annotations

from collections.abc import Callable

from wantabag.config import (
    AUDIO_NOTE,
    AUDIO_QUALITIES,
    RECOMMENDED_AUDIO,
    RECOMMENDED_VIDEO,
    VIDEO_LABELS,
    VIDEO_QUALITIES,
    Config,
    PlaylistEntry,
)
from wantabag.sources import parse_playlist_url


def ask(prompt: str, default: str, input_fn: Callable[[str], str]) -> str:
    answer = input_fn(f"{prompt} [{default}]: ").strip()
    return answer or default


def ask_yes_no(prompt: str, default: bool, input_fn: Callable[[str], str]) -> bool:
    hint = "Y/n" if default else "y/N"
    while True:
        answer = input_fn(f"{prompt} [{hint}]: ").strip().lower()
        if not answer:
            return default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False


def choose(
    prompt: str,
    options: list[tuple[str, str]],
    recommended: str,
    input_fn: Callable[[str], str],
    print_fn: Callable[[str], None],
) -> str:
    print_fn(prompt)
    for i, (key, label) in enumerate(options, 1):
        mark = " (recommended)" if key == recommended else ""
        print_fn(f"  {i}) {label}{mark}")
    keys = [k for k, _ in options]
    while True:
        answer = input_fn(f"Choose 1-{len(options)} [Enter = recommended]: ").strip()
        if not answer:
            return recommended
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return keys[int(answer) - 1]
        print_fn("Please enter one of the listed numbers.")


def run_setup(
    cfg: Config,
    input_fn: Callable[[str], str] = input,
    print_fn: Callable[[str], None] = print,
    read_name: Callable[[str], str] | None = None,
) -> tuple[Config, bool, bool]:
    """Update ``cfg`` from the user's answers.

    Returns it and whether to install the daily sync service and the web service.

    ``read_name`` looks up a playlist's own name so it can be offered as the folder name.
    """
    print_fn("wantabag setup - press Enter to accept the value in brackets.\n")
    cfg.output_dir = ask("Folder to save your music", cfg.output_dir, input_fn)

    print_fn("")
    audio = [(k, label) for k, (_, label) in AUDIO_QUALITIES.items()]
    audio[0] = ("v0", f"{AUDIO_QUALITIES['v0'][1]}")
    print_fn(AUDIO_NOTE)
    cfg.audio_quality = choose("Audio quality:", audio, RECOMMENDED_AUDIO, input_fn, print_fn)

    print_fn("")
    cfg.video_enabled = ask_yes_no("Also download music videos?", cfg.video_enabled, input_fn)
    if cfg.video_enabled:
        opts = [(k, VIDEO_LABELS[k]) for k in VIDEO_QUALITIES]
        cfg.video_quality = choose("Video quality:", opts, RECOMMENDED_VIDEO, input_fn, print_fn)

    print_fn("")
    if cfg.playlists:
        print_fn("Playlists you follow:")
        for p in cfg.playlists:
            print_fn(f"  - {p.name or p.url}")
    added_spotify = False
    while True:
        url = input_fn("Paste a playlist URL (empty to finish): ").strip()
        if not url:
            break
        try:
            ref = parse_playlist_url(url)
        except ValueError as e:
            print_fn(f"  {e}")
            continue
        if any(p.url == url for p in cfg.playlists):
            print_fn("  Already in the list.")
            continue
        default = ""
        if read_name:
            try:
                default = read_name(url)
            except Exception as e:  # noqa: BLE001
                print_fn(f"  Could not read the playlist ({e}); the name is filled in at sync.")
        name = ask("  Folder name", default, input_fn) if default else ""
        cfg.playlists.append(PlaylistEntry(url, name))
        added_spotify |= ref.source == "spotify"
        print_fn("  Added.")

    if added_spotify:
        print_fn(
            "\nSpotify API credentials are optional: your own client id/secret, "
            "only used for long playlists."
        )
        cfg.spotify_client_id = ask("Spotify client id", cfg.spotify_client_id or "", input_fn)
        if cfg.spotify_client_id:
            cfg.spotify_client_secret = ask(
                "Spotify client secret", cfg.spotify_client_secret or "", input_fn
            )

    install = False
    if cfg.playlists:
        print_fn("")
        install = ask_yes_no("Install daily sync service?", True, input_fn)
    print_fn("")
    install_web = ask_yes_no(
        "Run the web interface as a background service (http://127.0.0.1:8765)?", False, input_fn
    )
    return cfg, install, install_web
