"""User workspace: assistants (presets: role, knowledge, web access) and the prompt library ("/" commands)."""
import contextlib, os, re, threading, time, uuid

import config

FILE = os.path.join(config.DATA, "workspace.json")
_lock = threading.RLock()
EXAMPLES = {
    "assistants": [
        {"id": "translator", "name": "Traducteur", "emoji": "🌍", "web": False, "docs": [],
         "system": "Translate every message the user sends into the other language (French <-> English by default, or the "
                   "language they ask for). Keep the tone, the formatting and the meaning; give only the translation."},
        {"id": "coach-code", "name": "Relecteur de code", "emoji": "🧑‍💻", "web": False, "docs": [],
         "system": "You are a senior engineer reviewing code. Point out bugs first, then risky patterns, then style. "
                   "Be concrete: quote the line, explain why, propose the fix."},
    ],
    "prompts": [
        {"id": "resume", "command": "resume", "title": "Résumer", "content": "Résume ce texte en 5 points clés :\n\n"},
        {"id": "mail", "command": "mail", "title": "Écrire un e-mail",
         "content": "Écris un e-mail clair et poli à {{destinataire}} pour {{objet}}."},
        {"id": "explain", "command": "explique", "title": "Expliquer simplement",
         "content": "Explique-moi simplement, avec une analogie, : "},
    ],
}


EXAMPLES_EN = {
    "translator": {"name": "Translator"}, "coach-code": {"name": "Code reviewer"},
    "resume": {"command": "summary", "title": "Summarize", "content": "Summarize this text in 5 key points:\n\n"},
    "mail": {"title": "Write an e-mail", "content": "Write a clear and polite e-mail to {{recipient}} about {{subject}}."},
    "explain": {"command": "explain", "title": "Explain simply", "content": "Explain simply, with an analogy: "},
}


def _load():
    d = config.read_json(FILE)
    if not isinstance(d, dict):  # first start: examples in the language of the interface
        import harness
        en = harness.SETTINGS.get("ui_lang", "en") != "fr"
        d = {k: [dict(x) | (EXAMPLES_EN.get(x["id"], {}) if en else {}) for x in v] for k, v in EXAMPLES.items()}
    d.setdefault("assistants", [])
    d.setdefault("prompts", [])
    return d


def get():
    with _lock:
        return _load()


def assistant(aid):
    return next((a for a in get()["assistants"] if a["id"] == aid), None) if aid else None


def assistant_scope(aid):
    """Hold a selected preset stable while admitting a request that uses its documents."""
    return _lock if aid else contextlib.nullcontext()


def _clean(kind, item):
    if kind == "assistants":
        name = str(item.get("name") or "").strip()[:60]
        if not name:
            raise ValueError("nom manquant")
        return {"name": name, "emoji": str(item.get("emoji") or "")[:4], "system": str(item.get("system") or "")[:8000],
                "docs": [int(x) for x in item.get("docs") or []], "web": bool(item.get("web"))}
    cmd = re.sub(r"[^\w-]+", "", str(item.get("command") or "").lower())[:30]
    if not cmd or not str(item.get("content") or "").strip():
        raise ValueError("commande ou texte manquant")
    return {"command": cmd, "title": str(item.get("title") or cmd)[:80], "content": str(item["content"])[:8000]}


def save(kind, item):
    """Creates or updates an assistant / prompt; returns its id."""
    if kind not in ("assistants", "prompts"):
        raise ValueError("type inconnu")
    with _lock:
        d = _load()
        row = _clean(kind, item) | {"updated": time.time()}
        old = next((x for x in d[kind] if x["id"] == item.get("id")), None)
        if old:
            old.update(row)
            rid = old["id"]
        else:
            rid = uuid.uuid4().hex[:8]
            d[kind].append({"id": rid} | row)
        config.write_json(FILE, d)
        return rid


def delete(kind, rid):
    with _lock:
        d = _load()
        d[kind] = [x for x in d.get(kind, []) if x["id"] != rid]
        config.write_json(FILE, d)
