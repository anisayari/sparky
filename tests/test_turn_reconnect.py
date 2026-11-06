"""Exercise the normal HTTP routes with a dropped SSE socket and a live, counting tool."""
import json
import threading
import urllib.request

import harness
import mcp
import server
import turn_runtime
from test_server import base as base, call


def test_reconnect_without_resubmitting_a_tool(base, monkeypatch):
    sid = call(base, "/api/new", {})[1]["id"]
    session = server.get_session(sid)
    entered, release = threading.Event(), threading.Event()
    calls = []

    def tool(name, args, **kwargs):
        calls.append((name, args))
        entered.set()
        assert release.wait(6)
        return "done"

    actions = iter([{"action": "tool", "args": {"name": "counter", "arguments": {"n": 1}}},
                    {"action": "reply", "args": {"text": "Finished."}}])
    monkeypatch.setattr(session, "ask_llm", lambda: next(actions))
    monkeypatch.setattr(mcp, "call", tool)
    monkeypatch.setitem(server.llm_state, "name", "test-model")
    body = {"id": sid, "text": "count", "turnId": "count-once"}
    req = urllib.request.Request(base + "/api/chat", json.dumps(body).encode(), {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            assert response.readline().startswith(b"data: ")
        assert entered.wait(4)
        state = call(base, "/api/state?id=" + sid)[1]
        assert state["turn"]["running"] and state["turn"]["id"] == "count-once"
        page = "/api/turn/events?id=" + sid + "&turnId=count-once&after="
        received = call(base, page + "0")[1]
        assert any(e.get("name") == "tool" for e in received["events"])
        assert call(base, page + "0", headers={"Origin": "https://example.invalid"})[0] == 403
    finally:
        release.set()
    cursor = received["cursor"]
    while True:
        page_result = call(base, page + str(cursor))[1]
        if page_result.get("reset") or not page_result["running"]:
            break
        cursor = page_result["cursor"]
    state = call(base, "/api/state?id=" + sid)[1]
    assert not state["turn"]["running"] and state["events"][-1]["status"] == "completed"
    assert [e["text"] for e in state["events"] if e["type"] == "reply"] == ["Finished."]
    assert call(base, "/api/chat", body)[1].count(b'"duplicate"') == 1
    # A browser retry even after a restart must not repeat the tool.
    restored = harness.Session.load(sid)
    assert restored.turn("count", [], None, None, lambda e: None, turn_id="count-once") == "duplicate"
    assert calls == [("counter", {"n": 1})]


def test_expired_cursors_require_a_fresh_snapshot():
    feed = turn_runtime.TurnFeed()
    feed.begin("first")
    for i in range(100):
        feed.publish({"type": "typing", "text": str(i)})
    assert feed.poll("first", 0) == {"reset": True}
    assert feed.poll("first", 98)["events"] == [{"type": "typing", "text": "98"}, {"type": "typing", "text": "99"}]
    feed.begin("second")
    assert feed.poll("first", 100) == {"reset": True}


def test_local_turn_cannot_change_provider_during_tool(monkeypatch):
    session = harness.Session(ephemeral=True)
    monkeypatch.setitem(harness.SETTINGS, "llm", "local-choice")
    calls = []

    def choose():
        harness.cloud_model()  # admission path used by both transports
        calls.append(harness.SETTINGS["llm"])
        return {"action": "tool", "args": {"name": "switch", "arguments": {}}}

    def switch(*args, **kwargs):
        monkeypatch.setitem(harness.SETTINGS, "llm", "another-choice")
        return "changed"

    monkeypatch.setattr(session, "ask_llm", choose)
    monkeypatch.setattr(mcp, "call", switch)
    events = []
    assert session.turn("stay local", [], None, None, events.append) == "failed"
    assert calls == ["local-choice"] and "modèle prévu" in events[-2]["text"]


def test_old_document_preview_is_unavailable_after_revocation(monkeypatch):
    import rag
    s = harness.Session(ephemeral=True)
    references = [{"source_number": 1, "collection_id": 2, "doc_id": 3, "revision": "v1", "chunk_id": 4,
                   "doc": "document", "text": "private passage", "page": 3}]
    event = {"type": "sources", **s.document_sources(references)}
    s.config["docs"] = [2]
    monkeypatch.setattr(s, "doc_ids", lambda: s.config["docs"])
    monkeypatch.setattr(rag, "live_hits", lambda items, scope: references if 2 in scope and items else [])
    assert "private passage" in str(s.visible_events([event]))
    s.config["docs"] = []
    assert s.visible_events([event])[0]["items"] == []
