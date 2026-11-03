"""Document authorization at the point of transmission, including provider fallback attempts."""
import io
import json
import threading
import urllib.error
import urllib.request
from types import SimpleNamespace

import pytest

import cloud
import harness
import memory
import process_owner
import rag
import scheduler
import turn_runtime
import workflows
import workspace
from test_rag import wait


@pytest.fixture
def source(monkeypatch):
    monkeypatch.setattr(memory.EMB, "encode", lambda *a, **k: None)
    cid = rag.create_collection("Admission")
    did = rag.add_document(cid, "sample.txt", b"provenance-sentinel")
    wait(did)
    hit = rag.search("provenance", [cid])[0]
    s = harness.Session(ephemeral=True)
    s.config["docs"] = [cid]
    s.history.append({"role": "user", "raw": "Summarize", "text": "Summarize", "images": [], "rag_hits": [hit]})
    try:
        yield s, hit
    finally:
        rag.delete_collection(cid)


def route(monkeypatch, provider):
    monkeypatch.setattr(process_owner, "require_endpoint", lambda url: None)
    fake = {"provider": "openai", "model": "fixture"} if provider == "cloud" else None
    monkeypatch.setattr(harness, "cloud_model", lambda: fake)
    monkeypatch.setattr(cloud, "_headers", lambda pid: {"Content-Type": "application/json"})


@pytest.mark.parametrize(("provider", "change"), [("local", "deselect"), ("cloud", "delete"), ("cloud", "preset")])
def test_chat_revoked_after_fit_never_opens_transport(source, monkeypatch, provider, change):
    s, hit = source
    route(monkeypatch, provider)
    if change == "preset":
        s.config["docs"] = []
        s.config["assistant"] = workspace.save("assistants", {"name": "Preset", "docs": [hit["collection_id"]]})
    fit = turn_runtime.fit
    def revoke(*args):
        messages, limit = fit(*args)
        assert "provenance-sentinel" in str(messages)
        if change == "delete":
            rag.delete_document(hit["doc_id"])
        elif change == "preset":
            workspace.delete("assistants", s.config["assistant"])
        else:
            s.config["excluded_docs"] = [hit["collection_id"]]
        return messages, limit
    monkeypatch.setattr(turn_runtime, "fit", revoke)
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: pytest.fail("revoked payload dispatched"))
    with pytest.raises(ValueError, match="sources ont changé"):
        s.ask_llm()


def test_cloud_retry_rechecks_and_does_not_retry_revoked_data(source, monkeypatch):
    s, hit = source
    route(monkeypatch, "cloud")
    sent = []
    class Rejection(io.BytesIO):
        def read(self, *args):
            # The first request was admitted. Revocation wins before the fallback body is opened.
            rag.delete_document(hit["doc_id"])
            return b"unsupported optional parameter"
    def open_request(req, **kw):
        sent.append(json.loads(req.data))
        assert len(sent) == 1
        raise urllib.error.HTTPError(req.full_url, 400, "unsupported", {}, Rejection())
    monkeypatch.setattr(urllib.request, "urlopen", open_request)
    with pytest.raises(ValueError, match="sources ont changé"):
        s.ask_llm()
    assert len(sent) == 1 and "provenance-sentinel" in str(sent) and "_sources" not in str(sent)


def test_threaded_claude_turn_checks_selection_before_sdk_request(source, monkeypatch):
    pytest.importorskip("anthropic")
    s, _ = source
    sent, options, events = [], [], []
    sdk = SimpleNamespace(messages=SimpleNamespace(stream=lambda **kw: sent.append(kw)))
    sdk.with_options = lambda **kw: options.append(kw) or sdk
    monkeypatch.setattr(cloud, "_client", lambda: sdk)
    monkeypatch.setattr(harness, "cloud_model", lambda: {"provider": "anthropic", "model": "fixture"})
    fit = turn_runtime.fit
    def revoke(*args):
        result = fit(*args)
        s.config["docs"] = []
        return result
    monkeypatch.setattr(turn_runtime, "fit", revoke)
    assert s.turn("another question", [], None, None, events.append) == "failed"
    assert not sent and options == [{"max_retries": 0}]
    assert any("sources ont changé" in e.get("text", "") for e in events)


def test_revoked_document_in_discarded_history_does_not_block(source, monkeypatch):
    s, hit = source
    s.history.append({"role": "user", "raw": "hello", "text": "hello", "images": []})
    route(monkeypatch, "local")
    def drop_old(messages, *args):
        assert "provenance-sentinel" in str(messages[1])
        rag.delete_document(hit["doc_id"])
        return [messages[0], messages[-1]], 200
    monkeypatch.setattr(turn_runtime, "fit", drop_old)
    def open_request(req, **kw):
        assert "provenance-sentinel" not in str(req.data) and "_sources" not in str(req.data)
        reply = json.dumps({"action": "reply", "args": {"text": "OK"}})
        return io.BytesIO(("data: " + json.dumps({"choices": [{"delta": {"content": reply}}]}) + "\n").encode())
    monkeypatch.setattr(urllib.request, "urlopen", open_request)
    assert s.ask_llm()["args"]["text"] == "OK"


def test_after_open_revocation_does_not_wait_for_stream(source, monkeypatch):
    s, hit = source
    route(monkeypatch, "local")
    entered, release, received = threading.Event(), threading.Event(), []
    class Response(io.BytesIO):
        def __iter__(self):
            entered.set()
            assert release.wait(4)
            yield b'data: {"choices":[{"delta":{"content":"{\\"action\\":\\"reply\\",\\"args\\":{\\"text\\":\\"OK\\"}}"}}]}\n'
    def open_request(req, **kwargs):
        received.append(json.loads(req.data))
        return Response()
    monkeypatch.setattr(urllib.request, "urlopen", open_request)
    result = []
    reader = threading.Thread(target=lambda: result.append(s.ask_llm()))
    reader.start()
    try:
        assert entered.wait(2)
        writer = threading.Thread(target=rag.delete_document, args=(hit["doc_id"],))
        writer.start()
        writer.join(2)
        assert not writer.is_alive()
    finally:
        release.set()
        reader.join(5)
    assert len(received) == 1 and result[0]["args"]["text"] == "OK"


@pytest.mark.parametrize(("provider", "change"), [("local", "delete"), ("cloud", "cancel")])
def test_workflow_revoked_between_fit_and_open(source, monkeypatch, tmp_path, provider, change):
    _, hit = source
    route(monkeypatch, provider)
    monkeypatch.setattr(scheduler, "FILE", str(tmp_path / "tasks.json"))
    monkeypatch.setitem(harness.SETTINGS, "llm", "fixture")
    task = scheduler.save_task({"prompt": "provenance", "collection": hit["collection_id"],
                                "when": {"type": "interval", "minutes": 60}})
    run = workflows.new_run()
    run.update(phase="summarize", sources=[hit], model="fixture")
    scheduler._update(task["id"], run=run)
    fit = turn_runtime.fit
    def revoke(*args):
        messages, limit = fit(*args)
        assert "provenance-sentinel" in str(messages)
        if change == "cancel":
            scheduler.delete(task["id"])
        else:
            rag.delete_document(hit["doc_id"])
        return messages, limit
    monkeypatch.setattr(turn_runtime, "fit", revoke)
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: pytest.fail("revoked summary dispatched"))
    with pytest.raises(ValueError, match="sources ont changé"):
        workflows.summarize(task, run, lambda *a, **kw: None)
