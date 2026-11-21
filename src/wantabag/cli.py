"""Command line interface."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from wantabag import __version__, searxng
from wantabag.config import (
    AUDIO_QUALITIES,
    VIDEO_QUALITIES,
    Config,
    PlaylistEntry,
    load_config,
    save_config,
)
from wantabag.download import Downloader, check_ffmpeg
from wantabag.files import playlist_dir
from wantabag.http import make_session
from wantabag.models import Playlist
from wantabag.search import make_searcher
from wantabag.service import Service, ServiceError, web_spec
from wantabag.setup_wizard import run_setup
from wantabag.sources import parse_playlist_url, read_playlist
from wantabag.state import LockBusyError, State, run_lock
from wantabag.sync import sync_playlist
from wantabag.web.jobs import JOB_ERRORS

KNOWN_ERRORS = (*JOB_ERRORS, LockBusyError, ServiceError)


class CliError(Exception):
    pass


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="wantabag",
        description="Turn Apple Music and Spotify playlists into MP3 files you own.",
    )
    p.add_argument("--version", action="version", version=f"wantabag {__version__}")
    p.add_argument("-v", "--verbose", action="store_true", help="show yt-dlp output")
    sub = p.add_subparsers(dest="command", required=True, metavar="command")

    get = sub.add_parser("get", help="download a playlist once, without saving it")
    get.add_argument("url")
    get.add_argument("--out", help="destination folder (default: from config)")
    get.add_argument("--name", help="folder name (default: the playlist's name)")
    get.add_argument("--video", action=argparse.BooleanOptionalAction, default=None)
    get.add_argument("--audio-quality", choices=list(AUDIO_QUALITIES))
    get.add_argument("--video-quality", choices=list(VIDEO_QUALITIES))
    get.add_argument("--limit", type=int, help="only process the first N tracks")
    get.add_argument("--dry-run", action="store_true", help="list the tracks, download nothing")

    add = sub.add_parser("add", help="follow a playlist")
    add.add_argument("url")
    add.add_argument("--name", help="folder name (default: the playlist's name)")
    sub.add_parser("list", help="show followed playlists")
    rm = sub.add_parser("remove", help="stop following a playlist")
    rm.add_argument("target", help="playlist URL or number from 'list'")
    sync = sub.add_parser("sync", help="download what is missing from followed playlists")
    sync.add_argument("target", nargs="?", help="playlist URL or number (default: all)")

    sub.add_parser("setup", help="interactive configuration")

    svc = sub.add_parser("service", help="background services (daily sync, web interface)")
    svc.add_argument("action", choices=["install", "uninstall", "status"])
    svc.add_argument("--web", action="store_true", help="manage the web interface service")
    svc.add_argument("--host", default="127.0.0.1", help="with --web: address to listen on")
    svc.add_argument("--port", type=int, default=8765, help="with --web: port to listen on")

    web = sub.add_parser("web", help="local web interface: paste a playlist, get a .zip")
    web.add_argument("--host", default="127.0.0.1", help="address to listen on (default: local)")
    web.add_argument("--port", type=int, default=8765)
    web.add_argument("--open", action="store_true", help="open the page in your browser")

    sx = sub.add_parser("searxng", help="optional SearXNG search backend")
    sx.add_argument("action", choices=["up", "down"])
    return p


def _require_ffmpeg() -> None:
    if hint := check_ffmpeg():
        raise CliError(hint)


def _sync_urls(
    cfg: Config, targets: list[tuple[str, str]], args: argparse.Namespace, out_dir: Path, session
) -> int:
    """Read and sync each (url, folder name) pair. Returns how many playlists could be read."""
    _require_ffmpeg()
    searcher = make_searcher(cfg.search_backend, cfg.searxng_url, session, args.verbose)
    downloader = Downloader(cfg.audio_quality, cfg.video_quality, args.verbose)
    read = 0
    with run_lock():
        state = State.load()
        for url, name in targets:
            try:
                playlist = read_playlist(url, cfg, session, name)
            except KNOWN_ERRORS as e:
                print(f"Error reading {url}: {e}", file=sys.stderr)
                continue
            read += 1
            print(f"\n{playlist.name}: {len(playlist.tracks)} tracks")
            if args.command == "sync":
                _remember_name(cfg, playlist)
            sync_playlist(
                playlist,
                cfg,
                state,
                searcher,
                downloader,
                session,
                out_dir,
                limit=getattr(args, "limit", None),
            )
    return read


def _remember_name(cfg: Config, playlist: Playlist) -> None:
    for entry in cfg.playlists:
        if entry.url == playlist.url and not entry.name:
            entry.name = playlist.name
            save_config(cfg)


def cmd_get(args: argparse.Namespace) -> int:
    cfg = load_config()
    if args.video is not None:
        cfg.video_enabled = args.video
    if args.audio_quality:
        cfg.audio_quality = args.audio_quality
    if args.video_quality:
        cfg.video_quality = args.video_quality
    parse_playlist_url(args.url)
    session = make_session()
    if args.dry_run:
        playlist = read_playlist(args.url, cfg, session, args.name or "")
        print(f"{playlist.name}: {len(playlist.tracks)} tracks")
        for i, t in enumerate(playlist.tracks, 1):
            print(f"{i:>4}. {t.label}")
        return 0
    out_dir = Path(args.out).expanduser() if args.out else cfg.out_path
    return 0 if _sync_urls(cfg, [(args.url, args.name or "")], args, out_dir, session) else 1


def cmd_add(args: argparse.Namespace) -> int:
    cfg = load_config()
    parse_playlist_url(args.url)
    if any(p.url == args.url for p in cfg.playlists):
        raise CliError("That playlist is already in the list.")
    playlist = read_playlist(args.url, cfg, make_session(), args.name or "")
    cfg.playlists.append(PlaylistEntry(args.url, playlist.name))
    save_config(cfg)
    print(f"Added '{playlist.name}' ({len(playlist.tracks)} tracks). Run 'wantabag sync'.")
    return 0


def _select(cfg: Config, target: str) -> PlaylistEntry:
    if target.isdigit() and 1 <= int(target) <= len(cfg.playlists):
        return cfg.playlists[int(target) - 1]
    for p in cfg.playlists:
        if p.url == target:
            return p
    raise CliError(f"No followed playlist matches {target!r}. See 'wantabag list'.")


def cmd_list(args: argparse.Namespace) -> int:
    cfg = load_config()
    if not cfg.playlists:
        print("No playlists yet. Add one with 'wantabag add <url>'.")
        return 0
    for i, p in enumerate(cfg.playlists, 1):
        source = parse_playlist_url(p.url).source
        count = "?"
        if p.name:
            directory = playlist_dir(cfg.out_path, Playlist(source, "", p.url, p.name, ()))
            count = str(len(list(directory.glob("*.mp3")))) if directory.is_dir() else "0"
        print(f"{i}. [{source}] {p.name or '(not synced yet)'} - {count} files - {p.url}")
    return 0


def cmd_remove(args: argparse.Namespace) -> int:
    cfg = load_config()
    entry = _select(cfg, args.target)
    cfg.playlists.remove(entry)
    save_config(cfg)
    print(f"Removed {entry.name or entry.url}. Downloaded files were kept.")
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    cfg = load_config()
    if not cfg.playlists:
        raise CliError("No playlists to sync. Add one with 'wantabag add <url>'.")
    entries = [_select(cfg, args.target)] if args.target else list(cfg.playlists)
    targets = [(e.url, e.name) for e in entries]
    read = _sync_urls(cfg, targets, args, cfg.out_path, make_session())
    return 0 if read else 1


def cmd_setup(args: argparse.Namespace) -> int:
    cfg = load_config()
    session = make_session()

    def read_name(url: str) -> str:
        return read_playlist(url, cfg, session).name

    cfg, install, install_web = run_setup(cfg, read_name=read_name)
    path = save_config(cfg)
    print(f"\nSaved {path}")
    if install:
        print(Service().install())
    if install_web:
        print(Service(spec=web_spec()).install())
    print("\nNext steps:")
    print("  wantabag sync     download your playlists now")
    print("  wantabag list     see what you follow")
    return 0


def cmd_service(args: argparse.Namespace) -> int:
    service = Service(spec=web_spec(args.host, args.port)) if args.web else Service()
    print(getattr(service, args.action)())
    return 0


def cmd_web(args: argparse.Namespace) -> int:
    from wantabag.web import server

    if not server.is_loopback(args.host):
        print(
            "Warning: the web interface has no authentication. "
            f"Anyone who can reach {args.host}:{args.port} can use it.",
            file=sys.stderr,
        )
    if hint := check_ffmpeg():
        print(f"Warning: {hint}", file=sys.stderr)
    try:
        return server.serve(args.host, args.port, args.open, args.verbose)
    except OSError as e:
        raise CliError(f"Could not listen on {args.host}:{args.port}: {e.strerror}") from e


def cmd_searxng(args: argparse.Namespace) -> int:
    cfg = load_config()
    print(searxng.up(cfg, make_session()) if args.action == "up" else searxng.down(cfg))
    return 0


COMMANDS = {
    "get": cmd_get,
    "add": cmd_add,
    "list": cmd_list,
    "remove": cmd_remove,
    "sync": cmd_sync,
    "setup": cmd_setup,
    "service": cmd_service,
    "web": cmd_web,
    "searxng": cmd_searxng,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return COMMANDS[args.command](args)
    except (CliError, *KNOWN_ERRORS) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted. Progress so far is saved.", file=sys.stderr)
        return 130
