"""Knowledge: persistent document collections (like Open WebUI / AnythingLLM workspaces) searched with citations.

Files are read with attach.py (PDFs page by page so each chunk knows its page), cut into ~900-char chunks,
embedded with the memory model (e5-small) when it is installed, and stored in data/knowledge.db.
Search is hybrid (cosine + BM25-like keywords) and falls back to keywords only without the model.
"""
import json, math, os, re, sqlite3, threading, time

import attach
import config
import memory

CHUNK, OVERLAP = 900, 150
MAX_CHARS = 5_000_000      # per document
_db = None
_lock = threading.RLock()


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
            # documents left half-indexed by a previous run cannot resume (their bytes are gone)
            _db.execute("UPDATE documents SET state='error', error='indexation interrompue, ajoute le fichier à nouveau' "
                        "WHERE state='indexing'")
            _db.commit()
        return _db


def _read(sql, args=()):
    with _lock:
        return db().execute(sql, args).fetchall()


def _write(sql, args=()):
    with _lock:
        c = db().execute(sql, args)
        db().commit()
        return c


# -- collections
def collections():
    rows = _read("SELECT d.*, (SELECT COUNT(*) FROM chunks WHERE doc_id=d.id) AS chunks FROM documents d "
                 "ORDER BY d.added")
    out = []
    for c in _read("SELECT * FROM collections ORDER BY created"):
        docs = [{k: r[k] for k in ("id", "name", "kind", "size", "chunks", "state", "error", "added")}
                for r in rows if r["collection_id"] == c["id"]]
        out.append({"id": c["id"], "name": c["name"], "docs": docs})
    return out


def create_collection(name):
    return _write("INSERT INTO collections(name,created) VALUES(?,?)",
                  ((name or "").strip() or "Sans titre", time.time())).lastrowid


def rename_collection(cid, name):
    if (name or "").strip():
        _write("UPDATE collections SET name=? WHERE id=?", (name.strip(), cid))


def delete_collection(cid):
    _write("DELETE FROM collections WHERE id=?", (cid,))


# -- documents
def add_document(collection_id, name, data, mime=""):
    """Registers the file and indexes it in the background; state goes 'indexing' -> 'ready' | 'error'."""
    if not _read("SELECT 1 FROM collections WHERE id=?", (collection_id,)):
        raise ValueError("collection inconnue")
    did = _write("INSERT INTO documents(collection_id,name,kind,size,added,state) VALUES(?,?,?,?,?,?)",
                 (collection_id, name, attach.kind_of(name, mime), len(data), time.time(), "indexing")).lastrowid
    if len(data) > attach.MAX_BYTES:
        _write("UPDATE documents SET state='error', error=? WHERE id=?", ("fichier trop gros (200 Mo max.)", did))
    elif not attach.start_reader(_ingest, did, name, data, mime):
        _write("UPDATE documents SET state='error', error=? WHERE id=?", (attach.READERS_BUSY, did))
    return did


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
        with _lock:
            if not db().execute("SELECT 1 FROM documents WHERE id=?", (did,)).fetchone():
                return  # deleted while indexing
            db().executemany("INSERT INTO chunks(doc_id,ord,page,text,vec) VALUES(?,?,?,?,?)",
                             [(did, i, p, c, json.dumps(vecs[i]) if vecs else None) for i, (p, c) in enumerate(pieces)])
            db().execute("UPDATE documents SET state='ready', chars=? WHERE id=?", (total, did))
            db().commit()
    except Exception as e:
        _write("UPDATE documents SET state='error', error=? WHERE id=?", (str(e) or e.__class__.__name__, did))


def reindex():
    """Embeds chunks stored before the embedding model was installed. Returns how many."""
    rows = _read("SELECT id,text FROM chunks WHERE vec IS NULL")
    if not rows or not memory.EMB.ready():
        return 0
    for i in range(0, len(rows), 32):
        part = rows[i:i + 32]
        vecs = memory.EMB.encode([r["text"] for r in part])
        if not vecs:
            return i
        with _lock:
            db().executemany("UPDATE chunks SET vec=? WHERE id=?", [(json.dumps(v), r["id"]) for r, v in zip(part, vecs)])
            db().commit()
    return len(rows)


# -- search
def _terms(t):
    return [w for w in memory.WORD.findall(t.lower()) if w not in memory.STOP]


def search(query, collection_ids=None, k=6):
    """Best chunks: [{doc_id, doc, page, text, score, collection}], hybrid cosine + BM25, near-duplicates removed."""
    sql = ("SELECT c.id, c.doc_id, c.page, c.text, c.vec, d.name AS doc, l.name AS collection, d.collection_id FROM chunks c "
           "JOIN documents d ON d.id=c.doc_id JOIN collections l ON l.id=d.collection_id WHERE d.state='ready'")
    args = []
    if collection_ids is not None:
        ids = [int(i) for i in collection_ids]
        if not ids:
            return []
        sql += f" AND d.collection_id IN ({','.join('?' * len(ids))})"
        args = ids
    rows = [dict(r) for r in _read(sql, args)]
    if not rows or not query.strip():
        return []
    # BM25 keywords
    q = set(_terms(query))
    toks = [_terms(r["text"]) for r in rows]
    avg = sum(map(len, toks)) / len(toks) or 1
    df = {w: sum(1 for t in toks if w in t) for w in q}
    kw = []
    for t in toks:
        s = 0.0
        for w in q:
            tf = t.count(w)
            if tf:
                idf = math.log(1 + (len(toks) - df[w] + 0.5) / (df[w] + 0.5))
                s += idf * tf * 2.2 / (tf + 1.2 * (0.25 + 0.75 * len(t) / avg))
        kw.append(s)
    top = max(kw) or 1
    qv = memory.EMB.encode([query], query=True) if any(r["vec"] for r in rows) else None
    for r, s in zip(rows, kw):
        s /= top
        if qv and r["vec"]:
            cos = sum(a * b for a, b in zip(qv[0], json.loads(r["vec"])))
            r["score"] = 0.7 * max(0.0, min(1.0, (cos - 0.75) / 0.2)) + 0.3 * s  # e5 cosines live in ~0.75..0.95
        else:
            r["score"] = s
    rows.sort(key=lambda r: -r["score"])
    hits, seen = [], []
    for r in rows:
        if r["score"] <= 0 or len(hits) >= k:
            break
        t = set(_terms(r["text"]))
        if any(len(t & s) / max(1, len(t | s)) > 0.8 for s in seen):
            continue  # same passage twice (overlap, duplicated file)
        seen.append(t)
        hits.append({"doc_id": r["doc_id"], "doc": r["doc"], "page": r["page"], "text": r["text"],
                     "score": round(r["score"], 4), "collection": r["collection"], "collection_id": r["collection_id"]})
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
