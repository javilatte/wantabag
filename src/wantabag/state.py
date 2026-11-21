"""Local state: per-track results, saved atomically."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

from wantabag.config import state_dir


class LockBusyError(Exception):
    pass


@contextlib.contextmanager
def run_lock(directory: Path | None = None) -> Iterator[None]:
    """Exclusive lock so the daily timer and a manual run never overlap."""
    directory = directory or state_dir()
    directory.mkdir(parents=True, exist_ok=True)
    with open(directory / "state.lock", "w") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            raise LockBusyError("another wantabag run is in progress") from e
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


class State:
    def __init__(self, path: Path, data: dict) -> None:
        self.path = path
        self._data = data

    @classmethod
    def load(cls, path: Path | None = None) -> State:
        path = path or state_dir() / "state.json"
        data: dict = {}
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                data = {}
        data.pop("apple_token", None)
        data.setdefault("version", 1)
        data.setdefault("tracks", {})
        return cls(path, data)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=1, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)

    def get(self, key: str) -> dict:
        return dict(self._data["tracks"].get(key, {}))

    def update(self, key: str, **fields: object) -> None:
        entry = self._data["tracks"].setdefault(key, {})
        entry.update(fields)
        entry["updated"] = datetime.now(UTC).isoformat(timespec="seconds")
        self.save()

    def bump_attempts(self, key: str) -> int:
        return int(self._data["tracks"].get(key, {}).get("attempts", 0)) + 1
