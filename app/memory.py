"""Sparky Bot long-term memory: SQLite store + hybrid retrieval (semantic embeddings, keywords,
importance, recency). Every memory is visible and editable by the user.
"""
import importlib.util, json, math, os, re, sqlite3, threading, time

import config

DB = os.path.join(config.DATA, "companion.db")
CATEGORIES = ("identity", "preference", "project", "people", "goal", "event", "style", "other")
EMBED_ID = "e5-small"


class Embedder:
    """multilingual-e5-small through transformers (CPU, ~20 ms per sentence). None until downloaded."""

    def __init__(self):
        self.model = self.tok = None
        self.load_error = ""
        self.lock = threading.Lock()

    @staticmethod
    def has_runtime():
        return all(importlib.util.find_spec(lib) is not None for lib in ("torch", "transformers"))

    def ready(self):
        import catalog
        m = catalog.BY_ID.get(EMBED_ID)
        return bool(m) and catalog.installed(m) and self.has_runtime() and not self.load_error

    def _load(self):
        import catalog
        from transformers import AutoModel, AutoTokenizer
        d = catalog.BY_ID[EMBED_ID]["dir"]
        self.tok = AutoTokenizer.from_pretrained(d, local_files_only=True)
        self.model = AutoModel.from_pretrained(d, local_files_only=True).eval()

    def encode(self, texts, query=False):
        if not self.ready():
            return None
        with self.lock:
            try:
                import torch
                if self.model is None:
                    self._load()
            except (ImportError, OSError, RuntimeError, ValueError) as error:
                self.load_error = str(error)
                return None
            pre = "query: " if query else "passage: "
            b = self.tok([pre + t for t in texts], padding=True, truncation=True, max_length=256, return_tensors="pt")
            with torch.no_grad():
                h = self.model(**b).last_hidden_state
            mask = b["attention_mask"].unsqueeze(-1).float()
            v = (h * mask).sum(1) / mask.sum(1)
            v = torch.nn.functional.normalize(v, dim=-1)
        return [x.tolist() for x in v]


EMB = Embedder()
WORD = re.compile(r"[a-zà-ÿ0-9]{3,}", re.I)
STOP = set("the and for you your are was with this that have has not but from they she him her his our out what when "
           "les des une est pas pour avec dans que qui sur son ses mes tes nous vous ils elle mais par cette tout plus".split())


def words(t):
    return {w for w in WORD.findall(t.lower()) if w not in STOP}


class Store:
    def __init__(self, path=DB):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.db.executescript("""
            CREATE TABLE IF NOT EXISTS memories(id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT, category TEXT, importance INTEGER,
                created REAL, updated REAL, last_used REAL, uses INTEGER DEFAULT 0, pinned INTEGER DEFAULT 0,
                source TEXT DEFAULT 'chat', vec TEXT);
            CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY, role TEXT, text TEXT, ts REAL);
            CREATE TABLE IF NOT EXISTS journal(id INTEGER PRIMARY KEY, ts REAL, text TEXT);
            CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS memory_sources(memory_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
                PRIMARY KEY(memory_id,message_id));""")
            schema = self.db.execute("SELECT sql FROM sqlite_master WHERE name='memories'").fetchone()[0]
            if "AUTOINCREMENT" not in schema.upper():
                # Keep legacy IDs. A stale tab must never be able to edit a replacement with a recycled ID.
                with self.db:
                    self.db.execute("BEGIN")
                    self.db.execute("ALTER TABLE memories RENAME TO old_memories")
                    self.db.execute(schema.replace("INTEGER PRIMARY KEY", "INTEGER PRIMARY KEY AUTOINCREMENT", 1))
                    self.db.execute("INSERT INTO memories SELECT * FROM old_memories")
                    self.db.execute("DROP TABLE old_memories")
            self.db.commit()

    # -- key/value (persona, style, bond, summary, check-in...)
    def get(self, key, default=None):
        r = self.db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(r["value"]) if r else default

    def set(self, key, value):
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO kv VALUES(?,?)", (key, json.dumps(value, ensure_ascii=False)))
            self.db.commit()

    # -- messages
    def add_message(self, role, text):
        with self.lock:
            c = self.db.execute("INSERT INTO messages(role,text,ts) VALUES(?,?,?)", (role, text, time.time()))
            self.db.commit()
            return c.lastrowid

    def messages(self, limit=200, after=0):
        rows = self.db.execute("SELECT * FROM messages WHERE id>? ORDER BY id DESC LIMIT ?", (after, limit)).fetchall()
        return [dict(r) for r in reversed(rows)]

    def next_messages(self, after, limit):
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM messages WHERE id>? ORDER BY id LIMIT ?", (after, limit)).fetchall()]

    def origins(self, mid):
        with self.lock:
            item = self.db.execute("SELECT source,updated FROM memories WHERE id=?", (mid,)).fetchone()
            if not item:
                return None
            messages = self.db.execute("SELECT m.id,m.ts,m.text FROM memory_sources s JOIN messages m "
                                       "ON m.id=s.message_id WHERE s.memory_id=? AND m.role='user' ORDER BY m.id", (mid,))
            return dict(item) | {"messages": [dict(r) for r in messages]}

    def forget_impact(self):
        with self.lock:
            return {"summary": self.get("summary", ""), "checkin": self.get("checkin", ""),
                    "journal": self.db.execute("SELECT COUNT(*) FROM journal").fetchone()[0],
                    "style": len(self.get("style", {}) or {}), "mood": bool(self.get("mood", ""))}

    # -- memories
    def memories(self):
        return [dict(r) | {"vec": None} for r in self.db.execute(
            "SELECT * FROM memories ORDER BY pinned DESC, importance DESC, updated DESC").fetchall()]

    def add_memory(self, text, category="other", importance=3, source="chat", pinned=0, message_id=None):
        text = text.strip()
        if not text:
            return None
        vec = EMB.encode([text])
        now = time.time()
        with self.lock:
            c = self.db.execute("INSERT INTO memories(text,category,importance,created,updated,last_used,source,pinned,vec) "
                                "VALUES(?,?,?,?,?,?,?,?,?)",
                                (text, category if category in CATEGORIES else "other", int(max(1, min(5, importance))),
                                 now, now, now, source, int(pinned), json.dumps(vec[0]) if vec else None))
            self._link(c.lastrowid, message_id)
            self.db.commit()
            return c.lastrowid

    def _link(self, mid, message_id):
        if message_id is not None:
            self.db.execute("INSERT OR IGNORE INTO memory_sources SELECT ?,id FROM messages WHERE id=? AND role='user'",
                            (mid, message_id))

    def update_memory(self, mid, text=None, category=None, importance=None, pinned=None, *, message_id=None, manual=False):
        r = self.db.execute("SELECT * FROM memories WHERE id=?", (mid,)).fetchone()
        if not r:
            return False
        text = (text or r["text"]).strip()
        vec = EMB.encode([text]) if text != r["text"] or not r["vec"] else None
        stored_vec = r["vec"] if text == r["text"] else None
        with self.lock:
            self.db.execute("UPDATE memories SET text=?, category=?, importance=?, pinned=?, updated=?, vec=? WHERE id=?",
                            (text, category if category in CATEGORIES else r["category"],
                             int(max(1, min(5, importance))) if importance else r["importance"],
                             int(pinned) if pinned is not None else r["pinned"], time.time(),
                             json.dumps(vec[0]) if vec else stored_vec, mid))
            if manual and text != r["text"]:
                self.db.execute("DELETE FROM memory_sources WHERE memory_id=?", (mid,))
                self.db.execute("UPDATE memories SET source='user' WHERE id=?", (mid,))
            self._link(mid, message_id)
            self.db.commit()
        return True

    def delete_memory(self, mid, *, merge_into=None):
        with self.lock, self.db:
            if merge_into is not None:
                self.db.execute("INSERT OR IGNORE INTO memory_sources SELECT ?,message_id FROM memory_sources "
                                "WHERE memory_id=?", (merge_into, mid))
            self.db.execute("DELETE FROM memory_sources WHERE memory_id=?", (mid,))
            self.db.execute("DELETE FROM memories WHERE id=?", (mid,))

    def forget_memory(self, mid):
        with self.lock, self.db:
            if not self.db.execute("DELETE FROM memories WHERE id=?", (mid,)).rowcount:
                return False
            self.db.execute("DELETE FROM memory_sources WHERE memory_id=?", (mid,))
            # Free-form derivatives have no per-fact attribution. Clear them together, atomically.
            self.db.execute("DELETE FROM kv WHERE key IN ('summary','checkin','mood','style')")
            self.db.execute("DELETE FROM journal")
            cutoff = self.db.execute("SELECT COALESCE(MAX(id),0) FROM messages").fetchone()[0]
            self.db.executemany("INSERT OR REPLACE INTO kv VALUES(?,?)",
                                [(k, str(cutoff)) for k in ("context_after", "reflected_upto")])
            pending = [i for i in self.get("forget_requests", []) if i != mid]
            self.db.execute("INSERT OR REPLACE INTO kv VALUES('forget_requests',?)", (json.dumps(pending),))
            return True

    def reindex(self):
        """Computes the missing vectors (memories saved before the embedding model was downloaded)."""
        rows = self.db.execute("SELECT id,text FROM memories WHERE vec IS NULL").fetchall()
        if not rows or not EMB.ready():
            return 0
        vecs = EMB.encode([r["text"] for r in rows])
        if not vecs:
            return 0
        with self.lock:
            for r, v in zip(rows, vecs):
                self.db.execute("UPDATE memories SET vec=? WHERE id=? AND text=?", (json.dumps(v), r["id"], r["text"]))
            self.db.commit()
        return len(rows)

    def nearest(self, text, exclude=None):
        """(id, cosine, text) of the closest memory by meaning, or None (no embeddings / no memories)."""
        qv = EMB.encode([text])
        if not qv:
            return None
        best = None
        for r in self.db.execute("SELECT id,text,vec FROM memories WHERE vec IS NOT NULL").fetchall():
            if r["id"] == exclude:
                continue
            cos = sum(a * b for a, b in zip(qv[0], json.loads(r["vec"])))
            if not best or cos > best[1]:
                best = (r["id"], cos, r["text"])
        return best

    def similarity(self, a, b):
        v = EMB.encode([a, b])
        if v:
            return sum(x * y for x, y in zip(v[0], v[1]))
        wa, wb = words(a), words(b)  # without embeddings: shared words (Jaccard), rescaled to a cosine-like range
        return 0.75 + 0.25 * len(wa & wb) / max(1, len(wa | wb))

    def search(self, query, k=12):
        """Hybrid ranking: meaning (cosine) or shared words, + importance, + recency. Pinned and identity always first."""
        rows = [dict(r) for r in self.db.execute("SELECT * FROM memories").fetchall()]
        if not rows:
            return []
        qv = EMB.encode([query], query=True) if query.strip() else None
        qw = words(query)
        now = time.time()
        for r in rows:
            if qv and r["vec"]:
                v = json.loads(r["vec"])
                sim = max(0.0, (sum(a * b for a, b in zip(qv[0], v)) - 0.80) / 0.15)  # e5 cosines live in ~0.80..0.95
            else:
                mw = words(r["text"])
                sim = len(qw & mw) / math.sqrt(len(mw) + 1) if qw else 0.0
            age = (now - max(r["updated"], r["last_used"] or 0)) / 86400
            r["score"] = 0.6 * min(1.0, sim) + 0.25 * r["importance"] / 5 + 0.15 * math.exp(-age / 30) \
                + (1.0 if r["pinned"] else 0) + (0.3 if r["category"] == "identity" else 0)
        rows.sort(key=lambda r: -r["score"])
        top = rows[:k]
        with self.lock:
            for r in top:
                self.db.execute("UPDATE memories SET last_used=?, uses=uses+1 WHERE id=?", (now, r["id"]))
            self.db.commit()
        return [{k2: r[k2] for k2 in ("id", "text", "category", "importance", "pinned")} for r in top]

    # -- journal
    def add_journal(self, text):
        with self.lock:
            self.db.execute("INSERT INTO journal(ts,text) VALUES(?,?)", (time.time(), text))
            self.db.commit()

    def journal(self, limit=30):
        return [dict(r) for r in self.db.execute("SELECT * FROM journal ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]

    def forget_all(self):
        with self.lock:
            for t in ("memories", "memory_sources", "messages", "journal", "kv"):
                self.db.execute(f"DELETE FROM {t}")
            self.db.commit()
