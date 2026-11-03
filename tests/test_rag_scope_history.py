"""Only currently selected knowledge collections reach later model requests."""
import json
from types import SimpleNamespace

import harness
import rag


def sent_text(session):
    return json.dumps(session.llm_messages(), ensure_ascii=False)


def test_auto_and_explicit_results_follow_current_scope(monkeypatch):
    s = harness.Session(ephemeral=True)
    from test_rag import wait
    c7, c9 = (rag.create_collection(f"Scope {n}") for n in (7, 9))
    for n, cid in ((7, c7), (9, c9)):
        wait(rag.add_document(cid, f"document-{n}.txt", f"private-passage-{n}".encode()))
    hits = [rag.search("private", [cid])[0] for cid in (c7, c9)]
    s.config["docs"] = [c7, c9]
    monkeypatch.setattr(rag, "search", lambda *a, **kw: hits)
    s._start_turn("question", [], None, None, [], lambda e: None)
    s._on_info_tool(SimpleNamespace(info=0, spare=lambda: None), "docs", {"query": "details"}, lambda e: None)
    assert "private-passage-7" in sent_text(s)
    s.config["docs"] = [c9]
    rendered = sent_text(s)
    assert "private-passage-7" not in rendered and "[2] document-9" in rendered
    s.config["docs"] = []
    assert "private-passage" not in sent_text(s)
    rag.delete_collection(c7)
    rag.delete_collection(c9)


def test_revocation_while_search_returns_and_legacy_history(monkeypatch):
    s = harness.Session(ephemeral=True)
    cid = rag.create_collection("revoked")
    s.config["docs"] = [cid]
    def search(*args, **kwargs):
        s.config["docs"] = []
        return [{"collection_id": cid, "doc": "local", "text": "revoked-material"}]
    monkeypatch.setattr(rag, "search", search)
    s._start_turn("my question", [], None, None, [], lambda e: None)
    assert "revoked-material" not in sent_text(s)
    doc = rag.context_block([{"doc": "old", "text": "old-private-excerpt"}])
    marker = "Excerpts from the user's knowledge base:"
    original = "I typed " + marker
    note = "\n[Fichier joint : my visible attachment]"
    s.history = [{"role": "user", "raw": original, "note": note, "text": original + note + "\n[" + doc + "]"},
                 {"role": "user", "text": "[DOCUMENTS]\n" + doc}]
    rendered = sent_text(s)
    assert "old-private-excerpt" not in rendered and original in rendered and "my visible attachment" in rendered
    rag.delete_collection(cid)
