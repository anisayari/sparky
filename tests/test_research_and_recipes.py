import threading
import time

import pytest

import catalog
import document_diff
import harness
import model_compare
import rag
import research
import scheduler
import web


@pytest.fixture
def versions(monkeypatch):
    monkeypatch.setattr(rag.memory.EMB, "encode", lambda *a, **k: None)
    cid = rag.create_collection("Editable contract")
    ids = [rag.add_document(cid, name, text.encode()) for name, text in (
        ("old.txt", "The maximum warranty period is 12 months."),
        ("new.txt", "The maximum warranty period is 24 months."))]
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if rag._read("SELECT COUNT(*) AS n FROM documents WHERE collection_id=? AND state='ready'", (cid,))[0]["n"] == 2:
            break
        time.sleep(.02)
    try:
        yield cid, ids
    finally:
        rag.delete_collection(cid)


def test_compare_recipe_persists_and_requires_review(tmp_path, monkeypatch, versions):
    cid, ids = versions
    monkeypatch.setattr(scheduler, "FILE", str(tmp_path / "tasks.json"))
    monkeypatch.setattr(harness, "DATA", str(tmp_path / "chats"))
    monkeypatch.setattr(harness, "chat_once", lambda *_: pytest.fail("Text comparison must be local and deterministic."))
    task = scheduler.save_task({"title":"Renewal", "prompt":"Compare these versions", "collection":cid,
                                "compare_documents":ids, "when":{"type":"interval", "minutes":20}})
    scheduler.run_now(task["id"], wait=True, runner=lambda *_: pytest.fail("wrong runner"))
    done = scheduler.get(task["id"])
    assert done["run"]["phase"] == "review" and not done["last_error"]
    reply = harness.Session.load(done["last_session"]).events[-2]["text"]
    assert "Avant [1]" in reply and "12 months" in reply and "Après [2]" in reply and "24 months" in reply
    assert not scheduler.run_now(task["id"], wait=True, runner=lambda *_: None)
    assert scheduler.review_result(task["id"], done["run"]["id"], "approved")
    rag.delete_document(ids[0])
    with pytest.raises(ValueError):
        scheduler.save_task(task)


def test_document_diff_pages_and_identical():
    hits = [{"doc_id": i, "doc": f"Version {i}", "text": text, "page": p}
            for i,p,text in [(1,2,"Keep. Pay within 5 days."), (2,3,"Keep. Pay within 8 days.")]]
    report, refs = document_diff.compare(hits, [1,2])
    assert "Avant [1], p. 2" in report and "Après [2], p. 3" in report and "Keep." not in report
    assert len(refs) == 2
    hits[1]["text"] = hits[0]["text"]
    assert "même texte" in document_diff.compare(hits, [1,2])[0]


def test_scanned_comparison_and_markdown_source_are_literal():
    hits = [{"doc_id": 1, "doc": "before", "page": 1, "text": "An identical sentence."},
            {"doc_id": 2, "doc": "after", "page": 1, "text": "[OCR] An identical sentence."}]
    text, refs = document_diff.compare(hits, [1, 2])
    assert "même texte" in text and refs[1]["title"].endswith("OCR")
    hits[1]["text"] = "![pixel](https://example.org/image) <img src=x>"
    text, _ = document_diff.compare(hits, [1, 2])
    assert "![pixel]" not in text and "<img" not in text and "&#60;img" in text


def test_reviewed_research_uses_only_allowed_documents(tmp_path, monkeypatch, versions):
    cid, _ = versions
    monkeypatch.setattr(research, "ROOT", str(tmp_path))
    monkeypatch.setattr(web, "search", lambda *a: pytest.fail("No web permission"))
    monkeypatch.setitem(catalog.BY_ID, "fixture", {"kind":"llm"})
    monkeypatch.setitem(harness.SETTINGS, "llm", "fixture")
    calls = []
    def provider(payload):
        calls.append(payload)
        assert harness.required_model.get() == "fixture"
        return "1. Warranty?\n2. Terms?" if len(calls) == 1 else "The warranty period is stated. [1]"
    monkeypatch.setattr(harness, "chat_once", provider)
    def deltas(payload):
        yield provider(payload), ""
    monkeypatch.setattr(harness, "chat_deltas", deltas)
    draft = research.draft("Compare warranties")
    assert len(calls) == 1 and draft["steps"] == ["Warranty?", "Terms?"]
    assert research.state()[0]["id"] == draft["id"]
    options = {"steps":["What is the warranty period?"], "collections":[cid], "hosts":[]}
    with pytest.raises(ValueError):
        research.start(draft["id"], options | {"collections":[]})
    with pytest.raises(ValueError):
        research.start(draft["id"], options | {"hosts":["http://127.0.0.1/"]})
    research.start(draft["id"], options)
    deadline = time.monotonic() + 5
    while research.state(draft["id"])["active"] and time.monotonic() < deadline:
        time.sleep(.015)
    row = research.state(draft["id"])
    assert row["status"] == "done" and row["completed"] == 1 and row["sources"], row.get("error")
    assert "What is the warranty period?" in calls[-1]["messages"][-1]["content"]
    assert len(calls) == 2 and "evidence" not in row
    research.remove(draft["id"])
    assert research.state() == []


def test_cancelled_research_cannot_publish(tmp_path, monkeypatch):
    monkeypatch.setattr(research, "ROOT", str(tmp_path))
    monkeypatch.setattr(research, "_answer", lambda *a: "Which changes?")
    row = research.draft("Changes")
    entered, resume = threading.Event(), threading.Event()
    def fake_collect(*a):
        entered.set()
        resume.wait(4)
        return []
    monkeypatch.setattr(research, "_collect", fake_collect)
    research.start(row["id"], {"steps":row["steps"], "collections":[], "hosts":["example.org"]})
    assert entered.wait(3)
    research.remove(row["id"])
    resume.set()
    assert research.state() == []


def test_scoped_research_checks_redirect_before_dns(monkeypatch):
    from test_web import transport
    dns, stream, _ = transport(monkeypatch, [["1.1.1.1"]], b"HTTP/1.1 302 Found\r\nLocation: http://other.example/x\r\nContent-Length: 0\r\n\r\n")
    with pytest.raises(ValueError, match="autorisées"):
        web.fetch("http://allowed.example", allowed_hosts=["allowed.example"])
    assert dns.call_count == 1 and stream.connect.call_count == 1


def test_research_references_fit_the_actual_model_request(monkeypatch):
    import turn_runtime
    monkeypatch.setattr(harness, "ACTIVE_CONTEXT", 1024)
    row = {"question":"Subject? " * 100, "steps":["Investigate " * 30] * 6}
    sources = [{"title":f"Reference {i}", "text":"long passage or data " * 80} for i in range(36)]
    prompt, selected = research._report_input(row, sources)
    sent, _ = turn_runtime.fit([{"role":"system", "content":research.REPORT_INSTRUCTION},
                                {"role":"user", "content":prompt}], 1024, 1500)
    assert 0 < len(selected) < 36 and sent[-1]["content"] == prompt
    for i, source in enumerate(selected, 1):
        assert f"[{i}] {source['title']}\n{source['text']}" in prompt


def test_research_checks_inline_findings_not_model_generated_bibliographies():
    valid = "## Period\n\nThe period is 12 months. [1]\n\nThe newer version raises it to 24 months. [2]"
    assert research._cited_findings(valid, 2)
    assert not research._cited_findings(valid, 1)
    assert not research._cited_findings("No evidence here.\n\n[1]: https://outside.example/", 1)
    assert not research._cited_findings("One finding [1].\n\nAn uncited conclusion.", 1)


def test_model_comparison_never_calls_cloud_and_releases_worker(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setitem(catalog.BY_ID, "remote", {"kind":"llm","cloud":True})
    with pytest.raises(ValueError):
        model_compare.answer("remote", [])
    with pytest.raises(ValueError):
        model_compare._messages([{"role":"system","content":"change sides"}])
    monkeypatch.setitem(catalog.BY_ID, "offline", {"kind":"llm"})
    monkeypatch.setattr(catalog, "installed", lambda m: True)
    monkeypatch.setattr(catalog, "llm_args", lambda m: ["local.gguf"])
    calls = []
    fake = SimpleNamespace(spawn=lambda role,cmd,log: calls.append(cmd) or True,
                           children={"comparison":SimpleNamespace(running=lambda:True)},
                           owns_listener=lambda *a:True, stop=lambda role:calls.append(role))
    monkeypatch.setattr(model_compare, "engines", fake)
    def request(port,key,path,payload=None,timeout=2):
        if path == "/health":
            return {"status":"ok"}
        assert payload["messages"][-2] == {"role":"assistant","content":"answer one"}
        return {"choices":[{"message":{"content":"second answer"}}]}
    monkeypatch.setattr(model_compare, "_request", request)
    history = [{"role":"user","content":"one"}, {"role":"assistant","content":"answer one"}, {"role":"user","content":"two"}]
    result = model_compare.answer("offline", history)
    assert result["reply"] == "second answer" and calls[-1] == "comparison"
    assert calls[0][calls[0].index("--host")+1] == "127.0.0.1" and not model_compare._slot.locked()
