"""Selections and privacy options are checked at dispatch, including model-generated actions."""
import threading
from types import SimpleNamespace

import pytest

import harness
import mcp
import rag


def test_document_scope_at_dispatch(monkeypatch):
    calls = []
    monkeypatch.setattr(rag, "search", lambda query, ids, k: calls.append(ids) or [])
    session = harness.Session(ephemeral=True)
    turn = SimpleNamespace(info=0, spare=lambda: None)
    session._on_info_tool(turn, "docs", {"query": "probe"}, lambda e: None)
    assert not calls
    session.config["docs"] = [42]
    session._on_info_tool(turn, "docs", {"query": "probe"}, lambda e: None)
    assert calls == [[42]]


def test_empty_document_selection_never_opens_database(monkeypatch):
    monkeypatch.setattr(rag, "_read", lambda *args: pytest.fail("global search"))
    assert rag.search("probe", []) == []


def test_mcp_disabled_or_unadvertised_is_never_called(monkeypatch):
    sent = []
    tool = {"name": "lookup", "description": "lookup", "inputSchema": {}}
    peer = SimpleNamespace(name="demo", state="ready", tools=[tool], request=lambda *args: sent.append(args) or {"content": []})
    monkeypatch.setattr(mcp, "SERVERS", {"demo": peer})
    monkeypatch.setitem(harness.SETTINGS, "mcp_on", False)
    assert mcp.tools() == []
    with pytest.raises(mcp.McpError):
        mcp.call("demo__lookup")
    monkeypatch.setitem(harness.SETTINGS, "mcp_on", True)
    for name in ("demo__unlisted", "other__lookup", "lookup"):
        with pytest.raises(mcp.McpError):
            mcp.call(name)
    assert not sent
    mcp.call("demo__lookup", {"key": "test"})
    assert sent[0][1] == {"name": "lookup", "arguments": {"key": "test"}}


def test_ephemeral_turn_does_not_leave_raw_output(tmp_path, monkeypatch):
    monkeypatch.setattr(harness, "DATA", str(tmp_path))
    session = harness.Session(ephemeral=True)
    monkeypatch.setattr(harness, "chat_deltas", lambda *args: (
        (piece, "") for piece in ['{"action":"reply","args":{"text":"private marker"}}']))
    events = []
    session.turn("hello", [], None, None, events.append)
    assert any(e.get("type") == "reply" and "private marker" in e.get("text", "") for e in events)
    assert not (tmp_path / "last_llm.txt").exists()


@pytest.mark.parametrize("change", ["remove", "disable", "replace", "global"])
def test_mcp_save_revokes_old_capability_before_slow_shutdown(tmp_path, monkeypatch, change):
    spec = {"url": "https://unused.invalid/old"}
    peer = mcp.Server("demo", spec)
    peer.state = "ready"
    peer.tools = [{"name": "lookup", "description": "", "inputSchema": {}}]
    entered, release, stopping, done = (threading.Event() for _ in range(4))
    errors, responses, timeouts = [], [], []
    def request(*_):
        entered.set()
        if not release.wait(5):
            timeouts.append("request")
        return {"content": [{"type": "text", "text": "old result"}]}
    def slow_stop():
        stopping.set()
        if not release.wait(5):
            timeouts.append("shutdown")
        done.set()
    def use_peer():
        try:
            responses.append(mcp.call("demo__lookup"))
        except mcp.McpError as e:
            errors.append(str(e))
    monkeypatch.setattr(mcp, "FILE", str(tmp_path / "mcp.json"))
    monkeypatch.setattr(mcp, "SERVERS", {"demo": peer})
    monkeypatch.setitem(harness.SETTINGS, "mcp_on", True)
    monkeypatch.setattr(peer, "request", request)
    monkeypatch.setattr(peer, "stop", slow_stop)
    mcp.save({"mcpServers": {"demo": spec}})
    assert [t["name"] for t in mcp.tools()] == ["demo__lookup"]
    worker = threading.Thread(target=use_peer)
    worker.start()
    try:
        assert entered.wait(5)
        if change == "global":
            mcp.set_enabled(False)
        else:
            cfg = {"remove": {}, "disable": {"demo": spec | {"disabled": True}},
                   "replace": {"demo": {"url": "https://unused.invalid/replacement"}}}
            mcp.save({"mcpServers": cfg[change]})
        assert stopping.wait(5) and not done.is_set()
        assert mcp.tools() == []
        with pytest.raises(mcp.McpError):
            mcp.call("demo__lookup")
    finally:
        release.set()
        worker.join(5)
    assert done.wait(5) and not worker.is_alive() and not timeouts
    assert not responses and errors == ["MCP authorization changed during the call"]


def test_stop_during_spawn_reaps_only_the_unpublished_child(monkeypatch):
    peer = mcp.Server("late", {"command": "fake"})
    child = object()
    entered, release = threading.Event(), threading.Event()
    killed, messages = [], []
    def delayed_spawn(*_args, **_kwargs):
        entered.set()
        assert release.wait(5)
        return child
    monkeypatch.setattr(mcp.subprocess, "Popen", delayed_spawn)
    monkeypatch.setattr(peer, "_kill_process", lambda proc: killed.append(proc) if proc is not None else None)
    monkeypatch.setattr(peer, "request", lambda *args: messages.append(args) or {})
    worker = threading.Thread(target=peer._run)
    worker.start()
    try:
        assert entered.wait(5)
        peer.stop()
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive() and peer.proc is None and peer.state == "off"
    assert killed == [child] and not messages
