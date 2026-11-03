"""Only currently selected knowledge collections reach later model requests."""
import json
from types import SimpleNamespace

import harness
import rag


def sent_text(session):
    return json.dumps(session.llm_messages(), ensure_ascii=False)


def test_auto_and_explicit_results_follow_current_scope(monkeypatch):
    s = harness.Session(ephemeral=True)
    s.config["docs"] = [7, 9]
    hits = [{"collection_id": i, "doc": f"document-{i}", "text": f"private-passage-{i}"} for i in (7, 9)]
    monkeypatch.setattr(rag, "search", lambda *a, **kw: hits)
    s._start_turn("question", [], None, None, [], lambda e: None)
    s._on_info_tool(SimpleNamespace(info=0, spare=lambda: None), "docs", {"query": "details"}, lambda e: None)
    assert "private-passage-7" in sent_text(s)
    s.config["docs"] = [9]
    rendered = sent_text(s)
    assert "private-passage-7" not in rendered and "[2] document-9" in rendered
    s.config["docs"] = []
    assert "private-passage" not in sent_text(s)


def test_revocation_while_search_returns_and_legacy_history(monkeypatch):
    s = harness.Session(ephemeral=True)
    s.config["docs"] = [7]
    def search(*args, **kwargs):
        s.config["docs"] = []
        return [{"collection_id": 7, "doc": "local", "text": "revoked-material"}]
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
