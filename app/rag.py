"""Knowledge: persistent document collections (like Open WebUI / AnythingLLM workspaces) searched with citations.

Files are read with attach.py (PDFs page by page so each chunk knows its page), cut into ~900-char chunks,
embedded with the memory model (e5-small) when it is installed, and stored in data/knowledge.db.
Search is hybrid (cosine + BM25-like keywords) and falls back to keywords only without the model.
"""
import hashlib, heapq, json, os, re, sqlite3, threading, time, uuid

import attach
import config
import memory

CHUNK, OVERLAP = 900, 150
MAX_CHARS = 5_000_000      # per document
_db = None
_lock = threading.RLock()
_dispatcher = None
MAX_PENDING_BYTES = 512 * 2**20
MAX_PENDING_FILES = 128


def _next_id(conn):
    # Legacy rowids were positive and may refer to a deleted object. Never allocate in that domain.
    conn.execute("UPDATE knowledge_sequence SET value=value-1")
    return conn.execute("SELECT value FROM knowledge_sequence").fetchone()[0]


def _upgrade(conn):
    if conn.execute("PRAGMA user_version").fetchone()[0] >= 2:
        return
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("CREATE TABLE knowledge_sequence(value INTEGER NOT NULL)")
        conn.execute("INSERT INTO knowledge_sequence VALUES(0)")
        conn.execute("ALTER TABLE collections ADD COLUMN folder TEXT NOT NULL DEFAULT ''")
        conn.execute("ALTER TABLE collections ADD COLUMN sync_status TEXT NOT NULL DEFAULT ''")
        conn.execute("ALTER TABLE collections ADD COLUMN last_sync REAL")
        conn.execute("ALTER TABLE documents ADD COLUMN revision TEXT NOT NULL DEFAULT ''")
        conn.execute("ALTER TABLE documents ADD COLUMN source TEXT NOT NULL DEFAULT ''")
        conn.execute("ALTER TABLE documents ADD COLUMN fingerprint TEXT NOT NULL DEFAULT ''")
        conn.execute("ALTER TABLE documents ADD COLUMN sync_stamp TEXT NOT NULL DEFAULT ''")
        conn.execute("UPDATE documents SET revision=lower(hex(randomblob(16)))")
        # Re-select collections once: a legacy permission cannot distinguish the original owner of a reused ID.
        for cid, name, created in conn.execute("SELECT id,name,created FROM collections").fetchall():
            fresh = _next_id(conn)
            conn.execute("INSERT INTO collections(id,name,created) VALUES(?,?,?)", (fresh, name, created))
            conn.execute("UPDATE documents SET collection_id=? WHERE collection_id=?", (fresh, cid))
            conn.execute("DELETE FROM collections WHERE id=?", (cid,))
        conn.execute("CREATE TABLE pending_documents(doc_id INTEGER PRIMARY KEY REFERENCES documents(id) "
                     "ON DELETE CASCADE, body BLOB NOT NULL, mime TEXT NOT NULL)")
        conn.execute("CREATE INDEX documents_collection ON documents(collection_id)")
        conn.execute("CREATE VIRTUAL TABLE chunk_search USING fts5(text, content='chunks', content_rowid='id')")
        conn.execute("INSERT INTO chunk_search(chunk_search) VALUES('rebuild')")
        conn.execute("CREATE TRIGGER chunks_insert AFTER INSERT ON chunks BEGIN "
                     "INSERT INTO chunk_search(rowid,text) VALUES(new.id,new.text); END")
        conn.execute("CREATE TRIGGER chunks_delete AFTER DELETE ON chunks BEGIN "
                     "INSERT INTO chunk_search(chunk_search,rowid,text) VALUES('delete',old.id,old.text); END")
        conn.execute("UPDATE documents SET state='error', error='indexation interrompue, ajoute le fichier à nouveau' "
                     "WHERE state='indexing'")
        conn.execute("PRAGMA user_version=2")


def db():
    """Shared connection, opened on first use (config.DATA may be redirected by tests)."""
    global _db
    with _lock:
        if _db is None:
            os.makedirs(config.DATA, exist_ok=True)
            _db = sqlite3.connect(os.path.join(config.DATA, "knowledge.db"), check_same_thread=False)
            _db.row_factory = sqlite3.Row
            _db.executescript("""
            PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS collections(id INTEGER PRIMARY KEY, name TEXT, created REAL);
            CREATE TABLE IF NOT EXISTS documents(id INTEGER PRIMARY KEY, collection_id INTEGER REFERENCES collections(id)
                ON DELETE CASCADE, name TEXT, kind TEXT, size INTEGER, chars INTEGER DEFAULT 0, added REAL,
                state TEXT, error TEXT DEFAULT '');
            CREATE TABLE IF NOT EXISTS chunks(id INTEGER PRIMARY KEY, doc_id INTEGER REFERENCES documents(id)
                ON DELETE CASCADE, ord INTEGER, page INTEGER, text TEXT, vec TEXT);
            CREATE INDEX IF NOT EXISTS chunks_doc ON chunks(doc_id);""")
            try:
                _upgrade(_db)
            except Exception:
                _db.close()
                _db = None
                raise
        return _db


def _read(sql, args=()):
    with _lock:
        return db().execute(sql, args).fetchall()


def _write(sql, args=()):
    with _lock:
        c = db().execute(sql, args)
        db().commit()
        return c


def existing_collections(ids):
    ids = sorted(set(ids))
    if not ids:
        return []
    return [r["id"] for r in _read("SELECT id FROM collections WHERE id IN (" + ",".join("?" for _ in ids) +
                                   ") ORDER BY id", ids)]


# -- collections
def collections():
    rows = _read("SELECT d.*, (SELECT COUNT(*) FROM chunks WHERE doc_id=d.id) AS chunks FROM documents d "
                 "ORDER BY d.added")
    out = []
    for c in _read("SELECT * FROM collections ORDER BY created"):
        docs = [{k: r[k] for k in ("id", "name", "kind", "size", "chunks", "state", "error", "added")}
                for r in rows if r["collection_id"] == c["id"]]
        out.append({k: c[k] for k in ("id", "name", "folder", "sync_status", "last_sync")} | {"docs": docs})
    return out


def create_collection(name):
    with _lock, db() as conn:
        cid = _next_id(conn)
        conn.execute("INSERT INTO collections(id,name,created) VALUES(?,?,?)",
                     (cid, (name or "").strip() or "Sans titre", time.time()))
        return cid


def rename_collection(cid, name):
    if (name or "").strip():
        _write("UPDATE collections SET name=? WHERE id=?", (name.strip(), cid))


def delete_collection(cid):
    _write("DELETE FROM collections WHERE id=?", (cid,))


# -- documents
def add_document(collection_id, name, data, mime="", *, source="", folder=None, sync_stamp=""):
    if len(data) > attach.MAX_BYTES:
        raise ValueError("fichier trop gros (200 Mo max.)")
    fingerprint = hashlib.sha256(data).hexdigest()
    with _lock, db() as conn:
        coll = conn.execute("SELECT folder FROM collections WHERE id=?", (collection_id,)).fetchone()
        if not coll or (folder is not None and folder != coll["folder"]):
            raise ValueError("collection ou dossier modifié")
        previous = conn.execute("SELECT id,fingerprint,state FROM documents WHERE collection_id=? AND source=? "
                                "AND name=? ORDER BY added DESC LIMIT 1", (collection_id, source, name)).fetchone()
        if previous and previous["fingerprint"] == fingerprint and previous["state"] != "error":
            return previous["id"]
        if previous:
            conn.execute("DELETE FROM documents WHERE id=?", (previous["id"],))
        count, used = conn.execute("SELECT COUNT(*),COALESCE(SUM(length(body)),0) FROM pending_documents").fetchone()
        if count >= MAX_PENDING_FILES or used + len(data) > MAX_PENDING_BYTES:
            raise ValueError("file d’indexation pleine, réessayez après les imports en cours")
        did = _next_id(conn)
        conn.execute("INSERT INTO documents(id,collection_id,name,kind,size,added,state,revision,source,fingerprint,sync_stamp) "
                     "VALUES(?,?,?,?,?,?,'indexing',?,?,?,?)",
                     (did, collection_id, name, attach.kind_of(name, mime), len(data), time.time(),
                      uuid.uuid4().hex, source, fingerprint, sync_stamp))
        conn.execute("INSERT INTO pending_documents VALUES(?,?,?)", (did, data, mime))
    resume()
    return did


def resume():
    global _dispatcher
    with _lock:
        if _dispatcher is None and db().execute("SELECT 1 FROM pending_documents LIMIT 1").fetchone():
            _dispatcher = threading.Thread(target=_drain, daemon=True, name="knowledge-ingest")
            try:
                _dispatcher.start()
            except RuntimeError:
                _dispatcher = None
                raise


def _drain():
    global _dispatcher
    while True:
        with _lock:
            row = db().execute("SELECT doc_id FROM pending_documents ORDER BY doc_id DESC LIMIT 1").fetchone()
            if row is None:
                _dispatcher = None
                return
        done = threading.Event()
        if attach.start_reader(_read_pending, row["doc_id"], done):
            done.wait()
        else:
            time.sleep(0.1)


def _read_pending(did, done):
    try:
        rows = _read("SELECT d.name,p.body,p.mime FROM pending_documents p JOIN documents d ON d.id=p.doc_id "
                     "WHERE d.id=?", (did,))
        if rows:
            r = rows[0]
            _ingest(did, r["name"], r["body"], r["mime"])
    finally:
        done.set()


def delete_document(did):
    _write("DELETE FROM documents WHERE id=?", (did,))


def _pages(name, data, mime):
    """[(page or None, text)] using attach.py readers; PDFs are split per page."""
    k, ext = attach.kind_of(name, mime), os.path.splitext(name.lower())[1]
    if k == "pdf":
        try:
            from pypdf import PdfReader
        except ImportError:
            raise RuntimeError("lecture des PDF indisponible (relance l'installation)") from None
        import io
        pages = [(i + 1, (p.extract_text() or "").strip()) for i, p in enumerate(PdfReader(io.BytesIO(data)).pages)]
        if not any(t for _, t in pages):
            raise RuntimeError("PDF scanné sans texte (images seulement)")
        return pages
    if k == "doc":
        return [(None, attach._office(data, ext))]
    if k in ("audio", "video"):
        return [(None, attach._audio(data))]
    if k == "text" or attach._looks_text(data):
        return [(None, attach._decode_text(data))]
    raise RuntimeError("format non lu : seuls le texte, les PDF, Office, l'audio et la vidéo sont pris en compte")


def chunk(text, size=CHUNK, overlap=OVERLAP):
    """~size-char pieces cut at paragraph, then sentence, then word boundaries; consecutive pieces share ~overlap chars."""
    text = re.sub(r"[ \t]+", " ", text.replace("\r\n", "\n")).strip()
    text = re.sub(r"\n{3,}", "\n\n", text)
    if len(text) <= size:
        return [text] if text else []
    out, start = [], 0
    while start < len(text):
        end = min(len(text), start + size)
        if end < len(text):  # best boundary in the second half of the window
            win = text[start:end]
            for sep in ("\n\n", "\n", ". ", "! ", "? ", "; ", " "):
                i = win.rfind(sep, size // 2)
                if i > 0:
                    end = start + i + len(sep)
                    break
        piece = text[start:end].strip()
        if piece:
            out.append(piece)
        if end >= len(text):
            break
        nxt = max(start + 1, end - overlap)  # back up by the overlap, then forward to a word start
        sp = text.find(" ", nxt, end)
        start = sp + 1 if sp != -1 else nxt
    return out


def _ingest(did, name, data, mime):
    try:
        pieces, total = [], 0
        for page, text in _pages(name, data, mime):
            text = text[:max(0, MAX_CHARS - total)]
            total += len(text)
            pieces += [(page, c) for c in chunk(text)]
        if not pieces:
            raise RuntimeError("fichier vide")
        vecs = []
        for i in range(0, len(pieces), 32):
            v = memory.EMB.encode([c for _, c in pieces[i:i + 32]], query=False)
            if v is None:
                vecs = None
                break
            vecs += v
        with _lock, db() as conn:
            if not db().execute("SELECT 1 FROM documents WHERE id=?", (did,)).fetchone():
                return  # deleted while indexing
            db().executemany("INSERT INTO chunks(doc_id,ord,page,text,vec) VALUES(?,?,?,?,?)",
                             [(did, i, p, c, json.dumps(vecs[i]) if vecs else None) for i, (p, c) in enumerate(pieces)])
            db().execute("UPDATE documents SET state='ready', chars=? WHERE id=?", (total, did))
            conn.execute("DELETE FROM pending_documents WHERE doc_id=?", (did,))
    except Exception as e:
        with _lock, db() as conn:
            conn.execute("UPDATE documents SET state='error', error=? WHERE id=?", (str(e) or e.__class__.__name__, did))
            conn.execute("DELETE FROM pending_documents WHERE doc_id=?", (did,))


def reindex():
    """Backfill a bounded batch; maintenance continues on subsequent passes."""
    if not memory.EMB.ready():
        return 0
    part = _read("SELECT id,doc_id,text FROM chunks WHERE vec IS NULL LIMIT 32")
    if not part:
        return 0
    vectors = memory.EMB.encode([r["text"] for r in part])
    if not vectors:
        return 0
    with _lock, db() as conn:
        conn.executemany("UPDATE chunks SET vec=? WHERE id=? AND doc_id=? AND vec IS NULL",
                         [(json.dumps(v), r["id"], r["doc_id"]) for r, v in zip(part, vectors)])
    return len(part)


def live_hits(hits, selected_ids):
    """Rebuild cached passages from the current index. Old caches without provenance expire."""
    selected = set(selected_ids)
    live = []
    with _lock:
        for number, hit in enumerate(hits, 1):
            if hit.get("collection_id") not in selected or not hit.get("revision"):
                continue
            row = db().execute("SELECT c.text,c.page,d.name AS doc,l.name AS collection FROM chunks c "
                               "JOIN documents d ON d.id=c.doc_id JOIN collections l ON l.id=d.collection_id "
                               "WHERE c.id=? AND d.id=? AND d.collection_id=? AND d.revision=? AND d.state='ready'",
                               (hit.get("chunk_id"), hit.get("doc_id"), hit["collection_id"], hit["revision"])).fetchone()
            if row:
                live.append(hit | dict(row) | {"source_number": hit.get("source_number", number)})
    return live


def _terms(t):
    return [w for w in memory.WORD.findall(t.lower()) if w not in memory.STOP]


def search(query, collection_ids=None, k=6):
    """Union indexed keyword candidates with the nearest vectors, including lexical misses."""
    if not query.strip() or k <= 0:
        return []
    ids = [] if collection_ids is None else list(dict.fromkeys(map(int, collection_ids)))
    if collection_ids is not None and not ids:
        return []
    scope = " AND d.collection_id IN (" + ",".join("?" for _ in ids) + ")" if ids else ""
    cols = ("c.id AS chunk_id,c.doc_id,c.page,c.text,c.vec,d.name AS doc,l.name AS collection,"
            "d.collection_id,d.revision")
    joins = (" FROM chunks c JOIN documents d ON d.id=c.doc_id JOIN collections l ON l.id=d.collection_id ")
    terms = list(dict.fromkeys(_terms(query)))[:32]
    limit = max(96, k * 8)
    lexical = []
    if terms:
        expr = " OR ".join('"' + t + '"' for t in terms)
        lexical = _read("SELECT " + cols + ", rank AS kw" + joins + " JOIN chunk_search ON chunk_search.rowid=c.id "
                        "WHERE chunk_search MATCH ? AND d.state='ready'" + scope + " ORDER BY rank LIMIT ?",
                        [expr, *ids, limit])
    candidates = {r["chunk_id"]: dict(r) for r in lexical}
    has_vec = (" WHERE d.state='ready' AND c.vec IS NOT NULL" + scope)
    qv = None
    if memory.EMB.ready() and _read("SELECT 1" + joins + has_vec + " LIMIT 1", ids):
        qv = memory.EMB.encode([query], query=True)
    if qv:
        # Page through vectors only. Passage texts are loaded for shortlisted IDs.
        best, last_id = [], 0
        while True:
            batch = _read("SELECT c.id,c.vec" + joins + has_vec + " AND c.id>? ORDER BY c.id LIMIT 128", [*ids, last_id])
            if not batch:
                break
            for r in batch:
                sim = sum(a * b for a, b in zip(qv[0], json.loads(r["vec"])))
                item = (sim, r["id"])
                if len(best) < limit:
                    heapq.heappush(best, item)
                elif item > best[0]:
                    heapq.heapreplace(best, item)
            last_id = batch[-1]["id"]
        missing = [cid for _, cid in best if cid not in candidates]
        if missing:
            marks = ",".join("?" for _ in missing)
            for r in _read("SELECT " + cols + ",0 AS kw" + joins + has_vec + f" AND c.id IN ({marks})", [*ids, *missing]):
                candidates[r["chunk_id"]] = dict(r)
    if not candidates:
        return []
    rows = list(candidates.values())
    top = max(-r["kw"] for r in rows) or 1
    for r in rows:
        score = -r.pop("kw") / top
        vec = r.pop("vec")
        if qv and vec:
            cos = sum(a * b for a, b in zip(qv[0], json.loads(vec)))
            score = 0.7 * max(0.0, min(1.0, (cos - 0.75) / 0.2)) + 0.3 * score
        r["score"] = round(score, 4)
    hits, seen = [], []
    for r in sorted(rows, key=lambda row: -row["score"]):
        if r["score"] <= 0 or len(hits) >= k:
            break
        tokens = set(_terms(r["text"]))
        if any(len(tokens & prev) / max(1, len(tokens | prev)) > 0.8 for prev in seen):
            continue
        seen.append(tokens)
        hits.append(r)
    return hits


def context_block(hits):
    """Numbered sources for the LLM, with the instruction to cite them as [1], [2]."""
    if not hits:
        return ""
    src = "\n\n".join(f"[{h.get('source_number', i)}] {h['doc']}" + (f" (p. {h['page']})" if h.get("page") else "") + f": {h['text']}"
                      for i, h in enumerate(hits, 1))
    return ("Excerpts from the user's knowledge base:\n\n" + src +
            "\n\nUse these sources when they are relevant. Cite each fact you take from them with its number in square "
            "brackets, like [1] or [2][3], right after the sentence. Do not invent sources; if they do not contain the "
            "answer, say so.")
