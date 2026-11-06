import urllib.request

import pytest

import harness
import process_owner
import rag
import server
import turn_runtime
import workspace
from test_server import base as base, call


def test_project_membership_across_chats_and_updates(base, monkeypatch, tmp_path):
    monkeypatch.setattr(workspace, "FILE", str(tmp_path / "workspace.json"))
    cid = rag.create_collection("shared")
    try:
        item = {"name": "Renovation", "system": "Use euros", "docs": [cid]}
        code, created = call(base, "/api/workspace", {"what": "projects", "item": item})
        assert code == 200
        pid = created["id"]
        first, second = (call(base, "/api/new", {"config": {"project_space": pid}})[1]["id"] for _ in range(2))
        a, b = (server.get_session(sid) for sid in (first, second))
        assert a.doc_ids() == b.doc_ids() == [cid]
        assert "Use euros" in a.setup_prompt() and "Use euros" in b.setup_prompt()
        assert harness.Session.load(first).project_space()["id"] == pid
        assert {s["id"] for s in harness.Session.list_all() if s["project_space"] == pid} == {first, second}
        assert call(base, "/api/config", {"id": second, "changes": {"document_choice": {"id": cid, "selected": False}}})[0] == 200
        original = workspace.project(pid)
        assert call(base, "/api/workspace", {"what": "projects", "item": original | {"system": "Use francs"}})[0] == 200
        assert call(base, "/api/workspace", {"what": "projects", "item": original})[0] == 400
        assert a.doc_ids() == [cid] and b.doc_ids() == []
        assert "Use francs" in a.setup_prompt() and "Use euros" not in b.setup_prompt()
        assert call(base, "/api/config", {"id": first, "changes": {"project_space": None}})[0] == 200
        assert a.doc_ids() == [] and "Use francs" not in a.setup_prompt()
        assert call(base, "/api/workspace", {"what": "projects", "action": "delete", "id": pid})[0] == 200
        assert b.project_space() == {} and "Use francs" not in b.setup_prompt()
        assert call(base, "/api/config", {"id": first, "changes": {"project_space": pid}})[0] == 404
        assert call(base, "/api/workspace", {"what": "projects", "item": original})[0] == 400
    finally:
        rag.delete_collection(cid)


@pytest.mark.parametrize("change", ["remove", "detach", "edit"])
def test_revoked_project_instructions_never_dispatched(monkeypatch, tmp_path, change):
    monkeypatch.setattr(workspace, "FILE", str(tmp_path / "ws.json"))
    pid = workspace.save("projects", {"name": "Private", "system": "project-private-sentinel"})
    s = harness.Session(ephemeral=True)
    s.config["project_space"] = pid
    s.history.append({"role": "user", "raw": "hello", "text": "hello", "images": []})
    fit = turn_runtime.fit

    def revoke(messages, *args):
        assert "project-private-sentinel" in str(messages)
        if change == "remove":
            workspace.delete("projects", pid)
        elif change == "detach":
            s.config["project_space"] = None
        else:
            workspace.save("projects", workspace.project(pid) | {"system": "new instructions"})
        return fit(messages, *args)

    monkeypatch.setattr(turn_runtime, "fit", revoke)
    monkeypatch.setattr(harness, "cloud_model", lambda: None)
    monkeypatch.setattr(process_owner, "require_endpoint", lambda url: None)
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: pytest.fail("revoked data dispatched"))
    with pytest.raises(ValueError, match="projet a changé"):
        s.ask_llm()
