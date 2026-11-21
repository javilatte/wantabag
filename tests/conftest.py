import socket

import pytest

_real_connect = socket.socket.connect
_real_create = socket.create_connection
LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def _is_loopback(address) -> bool:
    return isinstance(address, tuple) and address[0] in LOOPBACK


@pytest.fixture(autouse=True)
def block_network(monkeypatch):
    """No outside network; loopback stays open so the web server can be tested."""

    def refuse(*args, **kwargs):
        raise RuntimeError("network access is blocked in tests")

    def connect(self, address):
        return _real_connect(self, address) if _is_loopback(address) else refuse()

    def create_connection(address, *args, **kwargs):
        return _real_create(address, *args, **kwargs) if _is_loopback(address) else refuse()

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket, "create_connection", create_connection)


@pytest.fixture(autouse=True)
def isolated_dirs(tmp_path, monkeypatch):
    """Keep config and state of every test inside tmp_path."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
