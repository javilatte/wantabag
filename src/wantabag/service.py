"""Daily sync service: systemd user timer on Linux, LaunchAgent on macOS."""

from __future__ import annotations

import os
import platform
import plistlib
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

UNIT_NAME = "wantabag-sync"
LABEL = "io.github.wantabag.sync"
Run = Callable[..., subprocess.CompletedProcess]


class ServiceError(Exception):
    pass


def sync_command() -> list[str]:
    return [sys.executable, "-m", "wantabag", "sync"]


@dataclass(frozen=True)
class ServiceSpec:
    """One installable background job: the daily sync (a timer) or the web UI (always on)."""

    unit: str
    label: str
    command: list[str]
    description: str
    log_name: str
    daily: bool

    @property
    def systemd_target(self) -> str:
        return f"{self.unit}.timer" if self.daily else f"{self.unit}.service"

    @property
    def install_hint(self) -> str:
        return "wantabag service install" + ("" if self.daily else " --web")


SYNC = ServiceSpec(
    UNIT_NAME,
    LABEL,
    sync_command(),
    "wantabag: sync followed playlists",
    "wantabag.log",
    daily=True,
)


def web_spec(host: str = "127.0.0.1", port: int = 8765) -> ServiceSpec:
    return ServiceSpec(
        "wantabag-web",
        "io.github.wantabag.web",
        [sys.executable, "-m", "wantabag", "web", "--host", host, "--port", str(port)],
        "wantabag: web interface",
        "wantabag-web.log",
        daily=False,
    )


def _quote(arg: str) -> str:
    return '"' + arg.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_unit(cmd: list[str], description: str = SYNC.description, daily: bool = True) -> str:
    exec_start = f"ExecStart={' '.join(_quote(a) for a in cmd)}\n"
    if daily:
        return f"[Unit]\nDescription={description}\n\n[Service]\nType=oneshot\n{exec_start}"
    return (
        f"[Unit]\nDescription={description}\n\n"
        f"[Service]\nType=simple\n{exec_start}Restart=on-failure\nRestartSec=5\n\n"
        "[Install]\nWantedBy=default.target\n"
    )


def render_timer() -> str:
    return (
        "[Unit]\n"
        "Description=Run wantabag sync daily\n\n"
        "[Timer]\n"
        "OnCalendar=daily\n"
        "Persistent=true\n"
        "RandomizedDelaySec=30min\n\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )


def render_plist(cmd: list[str], log_path: Path, label: str = LABEL, daily: bool = True) -> bytes:
    when = (
        {"StartCalendarInterval": {"Hour": 10, "Minute": 0}, "RunAtLoad": False}
        if daily
        else {"RunAtLoad": True, "KeepAlive": True}
    )
    return plistlib.dumps(
        {
            "Label": label,
            "ProgramArguments": cmd,
            **when,
            "EnvironmentVariables": {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"},
            "StandardOutPath": str(log_path),
            "StandardErrorPath": str(log_path),
        }
    )


def _check(run: Run, args: list[str], what: str) -> subprocess.CompletedProcess:
    try:
        proc = run(args, capture_output=True, text=True)
    except FileNotFoundError as e:
        raise ServiceError(f"{args[0]} not found; cannot {what}") from e
    if proc.returncode != 0:
        raise ServiceError(f"could not {what}: {(proc.stderr or proc.stdout).strip()}")
    return proc


class Service:
    def __init__(
        self,
        run: Run = subprocess.run,
        home: Path | None = None,
        system: str | None = None,
        uid: int | None = None,
        spec: ServiceSpec = SYNC,
    ) -> None:
        self.spec = spec
        self.run = run
        self.home = home or Path.home()
        self.system = system or platform.system()
        self.uid = os.getuid() if uid is None else uid
        if self.system not in ("Linux", "Darwin"):
            raise ServiceError(f"service is only supported on Linux and macOS, not {self.system}")

    @property
    def _systemd_dir(self) -> Path:
        base = os.environ.get("XDG_CONFIG_HOME") or self.home / ".config"
        return Path(base) / "systemd" / "user"

    @property
    def _plist(self) -> Path:
        return self.home / "Library" / "LaunchAgents" / f"{self.spec.label}.plist"

    @property
    def _log(self) -> Path:
        return self.home / "Library" / "Logs" / self.spec.log_name

    @property
    def _domain(self) -> str:
        return f"gui/{self.uid}"

    def install(self) -> str:
        spec = self.spec
        if self.system == "Linux":
            d = self._systemd_dir
            d.mkdir(parents=True, exist_ok=True)
            unit = render_unit(spec.command, spec.description, spec.daily)
            (d / f"{spec.unit}.service").write_text(unit, encoding="utf-8")
            if spec.daily:
                (d / f"{spec.unit}.timer").write_text(render_timer(), encoding="utf-8")
            _check(self.run, ["systemctl", "--user", "daemon-reload"], "reload systemd")
            _check(
                self.run,
                ["systemctl", "--user", "enable", "--now", spec.systemd_target],
                "enable the service",
            )
            what = "timer" if spec.daily else "service"
            return f"Installed systemd user {what} {spec.systemd_target}" + (
                " (daily)." if spec.daily else "."
            )
        self._log.parent.mkdir(parents=True, exist_ok=True)
        self._plist.parent.mkdir(parents=True, exist_ok=True)
        self._plist.write_bytes(render_plist(spec.command, self._log, spec.label, spec.daily))
        self.run(["launchctl", "bootout", self._domain, str(self._plist)], capture_output=True)
        _check(
            self.run,
            ["launchctl", "bootstrap", self._domain, str(self._plist)],
            "load the LaunchAgent",
        )
        when = "daily at 10:00" if spec.daily else "always on"
        return f"Installed LaunchAgent {spec.label} ({when}). Log: {self._log}"

    def uninstall(self) -> str:
        spec = self.spec
        if self.system == "Linux":
            self.run(
                ["systemctl", "--user", "disable", "--now", spec.systemd_target],
                capture_output=True,
            )
            for suffix in ("service", "timer"):
                (self._systemd_dir / f"{spec.unit}.{suffix}").unlink(missing_ok=True)
            self.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
            return f"Removed the {spec.unit} systemd user unit."
        self.run(["launchctl", "bootout", self._domain, str(self._plist)], capture_output=True)
        self._plist.unlink(missing_ok=True)
        return f"Removed the {spec.label} LaunchAgent."

    def status(self) -> str:
        spec = self.spec
        not_installed = f"Not installed. Run: {spec.install_hint}"
        if self.system == "Linux":
            if not (self._systemd_dir / spec.systemd_target).exists():
                return not_installed
            if spec.daily:
                head = self.run(
                    ["systemctl", "--user", "list-timers", spec.systemd_target, "--no-pager"],
                    capture_output=True,
                    text=True,
                )
            else:
                head = self.run(
                    ["systemctl", "--user", "is-active", spec.systemd_target],
                    capture_output=True,
                    text=True,
                )
            logs = self.run(
                ["journalctl", "--user", "-u", spec.unit, "-n", "20", "--no-pager"],
                capture_output=True,
                text=True,
            )
            return f"{head.stdout.strip()}\n\nRecent log:\n{logs.stdout.strip()}"
        if not self._plist.exists():
            return not_installed
        proc = self.run(
            ["launchctl", "print", f"{self._domain}/{spec.label}"], capture_output=True, text=True
        )
        state = "loaded" if proc.returncode == 0 else "installed but not loaded"
        return f"LaunchAgent {spec.label}: {state}\nLog: {self._log}"
