from __future__ import annotations

import ipaddress
import socket
from typing import Any

import pytest


@pytest.fixture(autouse=True)
def deny_external_network(monkeypatch: pytest.MonkeyPatch) -> None:
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex

    def is_local(address: object) -> bool:
        if isinstance(address, str):
            return True
        if not isinstance(address, tuple) or not address:
            return False
        host = address[0]
        if not isinstance(host, str):
            return False
        if host == "localhost":
            return True
        try:
            return ipaddress.ip_address(host).is_loopback
        except ValueError:
            return False

    def guarded_connect(self: socket.socket, address: Any) -> None:
        if not is_local(address):
            raise OSError("external network is disabled by deterministic conformance")
        original_connect(self, address)

    def guarded_connect_ex(self: socket.socket, address: Any) -> int:
        if not is_local(address):
            raise OSError("external network is disabled by deterministic conformance")
        return original_connect_ex(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
