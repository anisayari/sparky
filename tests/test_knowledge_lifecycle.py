"""Document lifecycle and user-visible permission boundaries, entirely on disposable data."""
import json
import sqlite3
from types import SimpleNamespace

import pytest

import harness
import rag
import rag_sync
import web
from test_rag import wait


def test_deleted_or_replaced_document_cannot_reenter_context(monkeypatch):
    cid = rag.create_collection("Confidential")
    s = harness.Session(ephemeral=True)
    monkeypatch.setattr(s, "assistant", lambda: {"docs": [cid], "web": True})
    first = rag.add_document(cid, "sample.txt", b"Confidential transient test marker.")
    wait(first)
    s._start_turn("transient", [], None, None, [], lambda _: None)
    assert "Confidential transient" in str(s.llm_messages())
    h = s.history[-1]["rag_hits"][0]
    assert not rag.live_hits([h | {"revision": "obsolete"}], [cid])
    assert rag.live_hits([h | {"text": "forged"}], [cid])[0]["text"] == h["text"]
    s.config.update(excluded_docs=[cid], web_blocked=True)
    assert not s.doc_ids() and not s.web_on()
    s.config["excluded_docs"] = []
    wait(rag.add_document(cid, "sample.txt", b"Completely different new contents."))
    assert not rag.live_hits([h], [cid])
    assert "Confidential transient" not in str(s.llm_messages())
    rag.delete_collection(cid)
    other = rag.create_collection("Replacement")
    try:
        assert other != cid and not s.doc_ids()
        assert not rag.live_hits([h], [other])
    finally:
        rag.delete_collection(other)


def test_migration_keeps_contents_and_expires_legacy_ids():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        PRAGMA foreign_keys=ON;
        CREATE TABLE collections(id INTEGER PRIMARY KEY, name TEXT, created REAL);
        CREATE TABLE documents(id INTEGER PRIMARY KEY,collection_id INTEGER REFERENCES collections(id) ON DELETE CASCADE,
          name TEXT,kind TEXT,size INTEGER,chars INTEGER,added REAL,state TEXT,error TEXT);
        CREATE TABLE chunks(id INTEGER PRIMARY KEY,doc_id INTEGER REFERENCES documents(id) ON DELETE CASCADE,
          ord INTEGER,page INTEGER,text TEXT,vec TEXT);
        INSERT INTO collections VALUES(1,'old',0);
        INSERT INTO documents(id,collection_id,name,state) VALUES(1,1,'notes','ready');
        INSERT INTO chunks(id,doc_id,text) VALUES(1,1,'historical payload');
    """)
    rag._upgrade(conn)
    rag._upgrade(conn)
    cid = conn.execute("SELECT collection_id FROM documents").fetchone()[0]
    assert cid < 0 and conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 1
    assert conn.execute("SELECT rowid FROM chunk_search WHERE chunk_search MATCH 'historical'").fetchone()[0] == 1
    conn.execute("DELETE FROM collections WHERE id=?", (cid,))
    assert not conn.execute("SELECT rowid FROM chunk_search WHERE chunk_search MATCH 'historical'").fetchone()
    assert rag._next_id(conn) < cid
    conn.close()


def test_interrupted_queue_is_resumed_without_original(monkeypatch):
    cid = rag.create_collection("Persisted queue")
    with monkeypatch.context() as m:
        m.setattr(rag, "resume", lambda: None)
        did = rag.add_document(cid, "saved.txt", b"Stored resumable upload.")
        assert rag._read("SELECT length(body) FROM pending_documents WHERE doc_id=?", (did,))[0][0] == 24
    rag.resume()
    assert wait(did)["state"] == "ready"
    assert not rag._read("SELECT 1 FROM pending_documents WHERE doc_id=?", (did,))
    rag.delete_collection(cid)


def test_shared_export_redacts_source_events_and_preserves_turns(monkeypatch):
    import server
    s = harness.Session(ephemeral=True)
    s.history = [{"role": "user", "text": "hello", "images": [], "rag_hits": [{"text": "private-marker"}]}]
    s.events = [{"type": "user", "text": "hello"}, {"type": "sources", "kind": "docs",
                "items": [{"n": 1, "title": "private-title", "text": "private-marker"}]},
                {"type": "reply", "text": "Visible facts [1]."}]
    s.turns = [(0, 0)]
    monkeypatch.setattr(server, "get_session", lambda _: s)
    result = []
    server.Handler.export(SimpleNamespace(query=lambda: {"id": s.id}, send=lambda *a: result.append(a)), "")
    out = json.loads(result[0][1])
    assert "private-marker" not in str(out) and "private-title" not in str(out)
    assert out["turns"] == [[0, 0]] and len(out["events"]) == 3
    assert out["events"][1]["items"] == [] and s.events[1]["items"]
    assert out["events"][2]["text"] == "Visible facts [1]."
    result.clear()
    server.Handler.export(SimpleNamespace(query=lambda: {"id": s.id, "fmt": "backup"}, send=lambda *a: result.append(a)), "")
    backup = json.loads(result[0][1])
    assert backup["purpose"] == "local-backup" and backup["events"][1]["items"][0]["text"] == "private-marker"
    import chat_import
    backup["config"] = {"web": True, "docs": [1], "computer": "full"}
    restored = chat_import.restore(backup)
    assert not restored.web_on() and not restored.doc_ids() and restored.config["computer"] == "off"
    assert "rag_hits" not in restored.history[0] and restored.events[1]["items"]
    restored.delete()


def test_document_and_web_citations_share_numbering(monkeypatch):
    cid = rag.create_collection("Sources")
    wait(rag.add_document(cid, "notes.txt", b"Zebras are striped."))
    s = harness.Session(ephemeral=True)
    s.config.update(docs=[cid], web=True)
    monkeypatch.setattr(web, "fetch", lambda *a: {"url": "https://example.org", "title": "Page", "text": "Compared."})
    events = []
    s._start_turn("Zebras https://example.org", [], None, None, [], events.append)
    s._on_info_tool(SimpleNamespace(info=0, spare=lambda: None), "docs", {"query": "Zebras"}, events.append)
    ids = [i["n"] for e in events if e["type"] == "sources" for i in e["items"]]
    assert ids == [1, 2, 3]
    text = str(s.llm_messages())
    assert "[1] notes.txt" in text and "[2] Page" in text and "[3] notes.txt" in text
    rag.delete_collection(cid)


def test_folder_update_rename_delete_unbind_and_confinement(tmp_path):
    root = tmp_path / "chosen"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside marker", encoding="utf-8")
    p = root / "notes.txt"
    p.write_text("original passage", encoding="utf-8")
    link = root / "outside.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        pass  # Windows without symlink privileges still exercises handle confinement
    cid = rag.create_collection("Synced")
    try:
        rag_sync.set_folder(cid, str(root))
        rag_sync.scan(cid, str(root))
        row, = rag._read("SELECT id FROM documents WHERE collection_id=?", (cid,))
        assert wait(row["id"])["state"] == "ready"
        hits = rag.search("original", [cid])
        assert hits and not rag.search("outside", [cid])
        p.write_text("replacement passage", encoding="utf-8")
        rag_sync.scan(cid, str(root))
        assert not rag.live_hits(hits, [cid])
        rows = rag._read("SELECT id FROM documents WHERE collection_id=?", (cid,))
        assert rows, rag._read("SELECT sync_status FROM collections WHERE id=?", (cid,))[0][0]
        did = rows[0][0]
        assert wait(did)["state"] == "ready" and rag.search("replacement", [cid])
        renamed = root / "renamed.txt"
        p.rename(renamed)
        rag_sync.scan(cid, str(root))
        assert rag._read("SELECT name FROM documents WHERE collection_id=?", (cid,))[0][0] == renamed.name
        renamed.unlink()
        rag_sync.scan(cid, str(root))
        assert not rag._read("SELECT 1 FROM documents WHERE collection_id=?", (cid,))
        rag_sync.set_folder(cid, "")
        assert not rag._read("SELECT folder FROM collections WHERE id=?", (cid,))[0][0]
        with pytest.raises(ValueError):
            rag_sync.set_folder(cid, ".")
    finally:
        rag.delete_collection(cid)

def test_navigation_uses_latest_response_and_blocks_sends_until_ready():
    """Exercise the production navigation function with deliberately reordered network promises."""
    import shutil
    import subprocess
    from pathlib import Path
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js unavailable")
    html = (Path(__file__).resolve().parents[1] / "app" / "index.html").read_text(encoding="utf-8")
    nav = html[html.index("let navigation=0;"):html.index("blank=function(eph)", html.index("let navigation=0;"))]
    # The same lock is used before sending from a keypress or the Send button.
    send = html[html.index("async function send(){"):html.index("async function send(){") + 1000]
    assert "classList.contains('loading-chat')" in send
    js = """
const assert=require('assert/strict'), pending=new Map(), configWrites=new Map(), classes=new Set();
const document={body:{classList:{add: k=>classes.add(k),remove: k=>classes.delete(k)}}};
const upd=()=>{}, setEph=()=>{}, resetArts=()=>{}, renderTools=()=>{}, down=()=>{}, loadList=()=>{},
      CFG0=()=>({}), ta={focus(){}}, log={}, localStorage={setItem(){}}, HELLO='';
const fetch=url=>new Promise(resolve=>pending.set(decodeURIComponent(url.split('=')[1]),resolve));
let open_,busy=false,SID='old',CFG={docs:['old']},T,UIDX,VERS,TBASE;
const answer=(id)=>pending.get(id)({ok:true,json:async()=>({config:{docs:[id]},events:[]})});
""" + nav + """
(async()=>{const first=open_('A'),second=open_('B');assert(classes.has('loading-chat'));assert.equal(SID,'old');
answer('B');await second;answer('A');await first;assert(!classes.has('loading-chat'));
assert.equal(SID,'B');assert.deepEqual(CFG.docs,['B']);})().catch(e=>{console.error(e);process.exit(1)});
"""
    subprocess.run([node, "-e", js], check=True, timeout=10)
