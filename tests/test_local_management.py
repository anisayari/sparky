"""The management plane is scoped to a local account and one HTTP origin."""
import http.client
import socket
import threading
from types import SimpleNamespace

import pytest

import local_access
import server


@pytest.fixture
def listener():
    with server.Server(("127.0.0.1", 0), server.Handler) as srv:
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        try:
            yield srv.server_address
        finally:
            srv.shutdown()
            thread.join(2)


def get(address, headers=None):
    conn = http.client.HTTPConnection(*address, timeout=3)
    try:
        conn.request("GET", "/api/sessions", headers=headers or {})
        return conn.getresponse().status
    finally:
        conn.close()


def test_plain_same_account_and_browser_origin(listener):
    host, port = listener
    assert get(listener) == 200
    assert get(listener, {"Origin": f"http://{host}:{port}"}) == 200
    assert get(listener, {"Origin": f"http://{host}:{port + 1}"}) == 403
    assert get(listener, {"Host": f"{host}:{port + 1}"}) == 403
    assert get(listener, {"Sec-Fetch-Site": "same-site"}) == 403


def test_management_rejects_unknown_owner(listener, monkeypatch):
    monkeypatch.setattr(local_access, "same_account", lambda conn: False)
    assert get(listener) == 403


def test_peer_identity_uses_both_ends_and_owner(monkeypatch):
    addr = lambda ip, port: SimpleNamespace(ip=ip, port=port)
    peer = SimpleNamespace(getpeername=lambda: ("127.0.0.1", 42000), getsockname=lambda: ("127.0.0.1", 7860))
    entry = SimpleNamespace(pid=100, laddr=addr("::ffff:127.0.0.1", 42000), raddr=addr("127.0.0.1", 7860))
    monkeypatch.setattr(local_access.psutil, "net_connections", lambda kind: [entry])
    owners = {None: "owner", 100: "elsewhere"}
    euid = 1000
    monkeypatch.setattr(local_access.os, "geteuid", lambda: euid, raising=False)
    monkeypatch.setattr(local_access.psutil, "Process", lambda pid=None: SimpleNamespace(
        username=lambda: owners[pid], uids=lambda: SimpleNamespace(effective=euid if owners[pid] == "owner" else 1001)))
    assert not local_access.same_account(peer)
    owners[100] = "owner"
    assert local_access.same_account(peer)
    entry.raddr = addr("127.0.0.1", 7861)
    assert not local_access.same_account(peer)


@pytest.mark.parametrize("header,expected", [(b"Origin: https://foreign.invalid\r\nContent-Length: 0", 403),
                                             (b"Content-Length: -1", 400),
                                             (b"Content-Length: 0\r\nContent-Length: 1", 400),
                                             (b"Transfer-Encoding: chunked", 400)])
def test_rejected_post_closes_without_dispatching_trailing_bytes(listener, header, expected):
    host, port = listener
    authority = f"Host: {host}:{port}\r\n".encode()
    with socket.create_connection(listener, timeout=3) as client:
        client.sendall(b"POST /api/sessions HTTP/1.1\r\n" + authority + header + b"\r\n\r\n"
                       + b"GET /api/sessions HTTP/1.1\r\n" + authority + b"Connection: close\r\n\r\n")
        with client.makefile("rb") as response:
            wire = response.read()
    assert wire.startswith(f"HTTP/1.1 {expected} ".encode())
    assert b"connection: close" in wire.lower() and wire.count(b"HTTP/1.1 ") == 1
