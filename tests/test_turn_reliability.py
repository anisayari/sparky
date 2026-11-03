import json
import http.server
import contextlib
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

import checker
import harness
import turn_runtime


def scripted(session, actions, text="hello", images=(), **kwargs):
    items = iter(actions)
    session.ask_llm = lambda: next(items)
    return session.turn(text, images, None, None, lambda e: None, **kwargs)


def reply(text):
    return {"action": "reply", "args": {"text": text}}


def file(text):
    return {"action": "artifact", "args": {"project": "web", "path": "index.html", "content": text}}


def test_switch_and_fork_restore_resources_and_remove_permissions(monkeypatch):
    monkeypatch.setitem(harness.SETTINGS, "autotest", False)
    s = harness.Session()
    im = Image.new("RGB", (8, 8), "red")
    assert scripted(s, [file("<p>one</p>"), reply("one")], images=[im]) == "completed"
    s.config.update(computer="ask", computer_allow=["write"], web=True)
    assert scripted(s, [file("<p>two</p>"), reply("two")], "next", [im]) == "completed"
    assert scripted(s, [file("<p>three</p>"), reply("three")], text=None, rewind=1) == "completed"
    assert s.projects["web"]["files"]["index.html"] == "<p>three</p>"
    s = harness.Session.load(s.id)
    s.switch_version(1, 0)
    assert s.projects["web"]["files"]["index.html"] == "<p>two</p>"
    assert s.events[-1]["status"] == "completed"
    fork = s.fork(0)
    assert fork.projects["web"]["files"]["index.html"] == "<p>one</p>"
    assert set(fork.images) == {1} and fork.config["computer"] == "off" and not fork.config["web"]
    assert not fork.config.get("computer_allow") and "three" not in str(fork.history)
    assert s.fork(0, after_reply=False).projects == {}
    src, new = Path(s.images[1]["path"]), Path(fork.images[1]["path"])
    assert new.read_bytes() == src.read_bytes() and new.parent != src.parent
    s.delete()
    assert new.exists() and harness.Session.load(fork.id).projects == fork.projects


def test_checkpoint_survives_interrupted_atomic_write(monkeypatch):
    s = harness.Session()
    scripted(s, [reply("saved")])
    f = Path(s.dir) / "session.json"
    baseline = f.read_bytes()

    def fail(src, dest):
        raise OSError("storage full")
    with monkeypatch.context() as m:
        m.setattr(harness.config.os, "replace", fail)
        with pytest.raises(OSError):
            s.save()
    assert f.read_bytes() == baseline
    state = json.loads(baseline)
    state["events"].pop()
    state["checkpoint"] = True
    harness.config.write_json(str(f), state)
    restored = harness.Session.load(s.id)
    assert restored.events[-1]["status"] == "interrupted"
    assert not json.loads(f.read_bytes())["checkpoint"]


def test_cancel_before_first_token_never_dispatches_a_late_action(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    def provider(body, think):
        entered.set()
        release.wait(5)
        yield json.dumps(file("<p>too late</p>")), ""
    monkeypatch.setattr(harness, "chat_deltas", provider)
    events, result = [], []
    s = harness.Session()
    worker = threading.Thread(target=lambda: result.append(s.turn("hello", [], None, None, events.append, turn_id="turn1")))
    worker.start()
    try:
        assert entered.wait(3)
        assert not s.cancel_turn("old") and s.cancel_turn("turn1")
        worker.join(2)
        assert result == ["cancelled"] and events[-1]["status"] == "cancelled"
        assert not s.projects and not s.cancel_turn("turn1")
    finally:
        release.set()
        worker.join(5)
    assert harness.Session.load(s.id).events[-1]["status"] == "cancelled"


def test_context_fits_even_for_a_large_attachment():
    messages = [{"role": "system", "content": "rules " * 1000},
                {"role": "assistant", "content": "old answer " * 3000},
                {"role": "user", "content": [{"type": "text", "text": "my latest request\n" + "é" * 60000}]}]
    bounded, tokens = turn_runtime.fit(messages, 4096, 14000)
    assert sum(turn_runtime.message_cost(m) for m in bounded) + tokens <= 4096
    assert bounded[-1]["content"][0]["text"].startswith("my latest request") and len(bounded) == 2
    assert len(messages[-1]["content"][0]["text"]) > 60000


def test_long_tool_history_preserves_last_human_request():
    s = harness.Session(ephemeral=True)
    s.history = [{"role": "user", "raw": "Keep the rocket blue", "text": "Keep the rocket blue", "images": []}]
    s.history.extend({"role": "user", "text": "tool output " * 200, "images": []} for _ in range(55))
    messages, anchor = s.llm_messages(with_anchor=True)
    bounded, tokens = turn_runtime.fit(messages, 4096, 8000, anchor)
    assert "Keep the rocket blue" in str(bounded) and sum(map(turn_runtime.message_cost, bounded)) + tokens <= 4096


@pytest.mark.parametrize("provider", ["local", "openai-compatible"])
def test_stop_closes_idle_event_stream_transport(monkeypatch, provider):
    import cloud
    import process_owner
    opened, eof = threading.Event(), threading.Event()
    class Provider(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.flush()
            opened.set()
            self.connection.settimeout(5)
            if self.connection.recv(1) == b"":
                eof.set()
        def log_message(self, *args):
            pass
    with http.server.ThreadingHTTPServer(("127.0.0.1", 0), Provider) as srv:
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            url = f"http://127.0.0.1:{srv.server_port}"
            if provider == "local":
                monkeypatch.setattr(harness, "LLM", url)
                monkeypatch.setattr(process_owner, "require_endpoint", lambda url: None)
                monkeypatch.setattr(harness, "cloud_model", lambda: None)
            else:
                monkeypatch.setattr(harness, "cloud_model", lambda: {"provider": "custom", "model": "fixture"})
                monkeypatch.setattr(cloud, "base_url", lambda pid: url)
                monkeypatch.setattr(cloud, "_headers", lambda pid: {"Content-Type": "application/json"})
            s, statuses = harness.Session(), []
            thread = threading.Thread(target=lambda: statuses.append(s.turn("text", [], None, None, lambda e: None,
                                                                           turn_id="stream")))
            thread.start()
            assert opened.wait(3) and s.cancel_turn("stream")
            thread.join(2)
            assert statuses == ["cancelled"] and eof.wait(2)
        finally:
            srv.shutdown()


def test_claude_stream_closes_after_cancellation(monkeypatch):
    import cloud
    entered, closed = threading.Event(), threading.Event()
    class Stream:
        def __iter__(self):
            entered.set()
            closed.wait(5)
            return iter(())
        def close(self):
            closed.set()
        def get_final_message(self):
            return SimpleNamespace(stop_reason="end_turn")
    client = SimpleNamespace(messages=SimpleNamespace(stream=lambda **kwargs: contextlib.nullcontext(Stream())))
    monkeypatch.setitem(sys.modules, "anthropic", SimpleNamespace())
    monkeypatch.setattr(cloud, "_client", lambda: client)
    monkeypatch.setattr(harness, "cloud_model", lambda: {"provider": "anthropic", "model": "fixture"})
    s, outcome = harness.Session(), []
    worker = threading.Thread(target=lambda: outcome.append(s.turn("hello", [], None, None, lambda e: None, turn_id="a")))
    worker.start()
    try:
        assert entered.wait(3) and s.cancel_turn("a")
        worker.join(2)
        assert outcome == ["cancelled"] and closed.is_set()
    finally:
        closed.set()
        worker.join(5)


def test_comfy_missing_job_times_out_and_only_removes_its_own_id(monkeypatch):
    calls = []
    times = iter((0, 2))
    monkeypatch.setattr(harness.time, "monotonic", lambda: next(times))
    def fake(url, payload=None, timeout=None):
        calls.append((url, payload, timeout))
        return {"prompt_id": "ours"}
    monkeypatch.setattr(harness, "http_json", fake)
    with pytest.raises(TimeoutError):
        harness.comfy_result({}, max_seconds=1)
    assert calls[-1][0].endswith("/api/jobs/ours/cancel") and not any("/interrupt" in x[0] for x in calls)


def test_relative_javascript_imports_are_checked_without_execution():
    if not checker.node_dir():
        pytest.skip("no Node.js static validator")
    p = {"entry": "src/main.mjs", "files": {"src/main.mjs":
         '// import "./ignore.js"\nexport {x} from "./dependency.mjs?v=1";'}}
    assert checker.check(p)["status"] == "failed"
    p["files"]["src/dependency.mjs"] = "export const x = 1;"
    assert checker.check(p)["status"] == "passed"
