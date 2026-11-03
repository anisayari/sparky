"""An old tab cannot renew capabilities and an ephemeral chat can revoke approvals in flight."""
import json
from pathlib import Path
import shutil
import subprocess
import threading
from types import SimpleNamespace

import pytest

import computer
import rag
import server
import workspace
from test_server import base as base, call


def test_ephemeral_revokes_open_card_and_stale_tab_cannot_regrant(base, monkeypatch):
    monkeypatch.setitem(server.llm_state, "name", None)
    monkeypatch.setattr(server, "wait_llm", lambda emit: None)
    called = []
    monkeypatch.setitem(computer.RUN, "list", lambda args: called.append(args))
    _, new = call(base, "/api/new", {"ephemeral": True, "config": {"computer": "ask_all", "web": True}})
    sid = new["id"]
    session = server.get_session(sid)
    stale = dict(session.config)
    assert session.ephemeral and stale["computer"] == "ask_all"
    ready = threading.Event()
    cards = []
    def capture(e):
        if e["type"] == "approve":
            cards.append(e["id"])
            ready.set()
    t = threading.Thread(target=session._on_computer,
                         args=(SimpleNamespace(computer=0, spare=lambda: None), {"tool": "list", "path": "."}, capture),
                         daemon=True)
    t.start()
    try:
        assert ready.wait(3)
        assert call(base, "/api/config", {"id": sid, "changes": {"computer": "off"}})[0] == 200
        t.join(3)
        assert not t.is_alive() and not called
        assert call(base, "/api/approve", {"id": sid, "aid": cards[0], "decision": "always"})[0] == 404
        assert call(base, "/api/config", {"id": sid, "config": stale})[0] == 409
        code, state = call(base, "/api/config", {"id": sid, "changes": {"system": "new prompt"}})
        assert code == 200 and state["config"]["computer"] == "off"
        assert call(base, "/api/chat", {"id": sid, "text": "hello", "config": stale})[0] == 200
        assert session.config["computer"] == "off" and session.config["system"] == "new prompt"
        assert session.config.get("computer_allow", []) == []
    finally:
        session.set_computer_level("off")
        t.join(3)


def test_initial_settings_only_for_new_chat_without_id(base, monkeypatch):
    monkeypatch.setitem(server.llm_state, "name", None)
    monkeypatch.setattr(server, "wait_llm", lambda emit: None)
    for extra, expected in (({}, "ask"), ({"id": "20990101-000000-abcd"}, "off")):
        code, stream = call(base, "/api/chat", {"ephemeral": True, "config": {"computer": "ask"}, "text": "hello", **extra})
        assert code == 200
        ack = json.loads(stream.decode().split("\n\n")[0][6:])
        assert server.get_session(ack["id"]).config["computer"] == expected


@pytest.mark.parametrize("inherited", [False, True], ids=["direct", "assistant"])
def test_document_toggle_preserves_other_tabs_revocation(base, inherited):
    first, second = (rag.create_collection(name) for name in ("X", "Y"))
    aid = workspace.save("assistants", {"name": "Scope test", "docs": [first, second]}) if inherited else None
    initial = {"assistant": aid, "docs": [] if inherited else [first]}
    sid = call(base, "/api/new", {"ephemeral": True, "config": initial})[1]["id"]
    s = server.get_session(sid)
    try:
        assert first in s.doc_ids()
        def choice(cid, selected):
            code, reply = call(base, "/api/config", {"id": sid, "changes": {"document_choice": {"id": cid, "selected": selected}}})
            assert code == 200, reply
        if inherited:
            choice(second, False)
        choice(first, False)   # tab A revokes X
        choice(second, True)   # tab B had a stale selection including X
        assert s.doc_ids() == [second] and first in s.config["excluded_docs"]
        assert (second not in s.config["docs"]) == inherited
        assert call(base, "/api/config", {"id": sid, "changes": {"docs": [first, second], "excluded_docs": []}})[0] == 400
        assert call(base, "/api/config", {"id": sid, "config": initial})[0] == 409
        assert s.doc_ids() == [second]
    finally:
        rag.delete_collection(first)
        rag.delete_collection(second)
        if aid:
            workspace.delete("assistants", aid)


def test_client_sends_only_changed_fields_and_waits_for_ephemeral_writes():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js unavailable")
    html = (Path(__file__).resolve().parents[1] / "app" / "index.html").read_text(encoding="utf-8")
    helpers = html[html.index("const configWrites="):html.index("function menuAt(", html.index("const configWrites="))]
    js = """
const assert=require('assert/strict'),calls=[],gates=[];
let SID=null, EPH=true, CFG={computer:'ask_all',system:''};
const renderTools=()=>{},CFG0=()=>({}),alert=e=>{throw new Error(e)};
function post(url,body){calls.push([url,structuredClone(body)]);return new Promise(ok=>gates.push(j=>ok({ok:true,json:async()=>j})))}
const settle=()=>new Promise(ok=>setImmediate(ok));
""" + helpers + """
(async()=>{const create=createChat();CFG.computer='off';saveCfg('computer');
assert.deepEqual(calls[0][1].config,{computer:'ask_all',system:''});
gates.shift()({id:'temporary'});await settle();
assert.deepEqual(calls[1][1],{id:'temporary',changes:{computer:'off'}});
CFG.system='new instruction';const write=saveCfg('system'),beforeSend=flushConfig(SID);
assert.equal(calls.length,2);
gates.shift()({config:{computer:'off',system:''}});await settle();
assert.deepEqual(calls[2][1],{id:'temporary',changes:{system:'new instruction'}});
gates.shift()({config:{computer:'off',system:'new instruction'}});
await Promise.all([create,write,beforeSend]);assert.equal(CFG.computer,'off');assert(!configWrites.size);
})().catch(e=>{console.error(e);process.exitCode=1});
"""
    subprocess.run([node, "-e", js], check=True, timeout=10)
