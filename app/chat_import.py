"""Read conversation exports as portable content. Permissions always come from the new local session."""
import base64
import io
import json
import math
import os
import re
import struct
import time
import uuid
from pathlib import PureWindowsPath

from PIL import Image

import harness


def require(condition):
    if not condition:
        raise ValueError("format de conversation invalide")


def typed(value, kind):
    require(isinstance(value, kind))
    return value


def integer(n, minimum=0):
    require(type(n) is int and minimum <= n <= 2**31 - 1)
    return n


def project_id(value):
    require(isinstance(value, str) and re.fullmatch(r"[a-z0-9][a-z0-9-]{0,39}", value))
    require(not PureWindowsPath(value).is_reserved())
    return value


def project_path(path):
    require(isinstance(path, str) and path and harness.clean_path(path) == path)
    require(all(not part.endswith(".") and not PureWindowsPath(part).is_reserved() for part in path.split("/")))
    return path


def attachments(items):
    for f in typed(items, list):
        typed(f, dict)
        typed(f.get("name"), str)
        typed(f.get("kind"), str)
    return items


def checked_events(events, artifacts):
    for e in typed(events, list):
        typ = typed(typed(e, dict).get("type"), str)
        if typ in ("ack", "typing", "read", "status", "reasoning"):
            # A live transport message has no place in a saved transcript. Keep its offset for undo.
            e.clear()
            e["type"] = "ignored"
            continue
        for key in ("text", "title", "output", "content", "language", "label", "summary", "preview"):
            if key in e:
                typed(e[key], str)
        for key in ("secs", "ts"):
            if key in e:
                require(type(e[key]) in (int, float) and 0 <= e[key] <= 2**53 and math.isfinite(e[key]))
        if typ in ("user", "reply"):
            typed(e.get("text"), str)
        if typ == "user":
            for i in typed(e.get("images", []), list):
                integer(i, 1)
            if e.get("maskFor") is not None:
                integer(e["maskFor"], 1)
            attachments(e.get("files", []))
        elif typ == "image":
            integer(e.get("id"), 1)
            for i in typed(e.get("refs", []), list):
                integer(i, 1)
        elif typ == "artifact":
            aid = project_id(e.get("id"))
            version = integer(e.get("version"), 1)
            require(version <= max(len(events), len(artifacts.get(aid, {}).get("versions", []))))
            require(e.get("kind") in ("html", "svg", "markdown", "code"))
            typed(e.get("content"), str)
        elif typ == "project":
            project_id(e.get("id"))
            project_path(e.get("path"))
            if e.get("entry") is not None:
                project_path(e["entry"])
            if "asset" in e:
                integer(e["asset"], 1)
            else:
                typed(e.get("content"), str)
            integer(e.get("version"), 1)
        elif typ == "test":
            integer(e.get("round", 0))
        elif typ == "tool":
            args = typed(e.get("args", {}), dict)
            if args.get("refs"):
                require(all(isinstance(ref, dict) for ref in typed(args["refs"], list)))
        elif typ == "sources":
            for source in typed(e.get("items"), list):
                integer(typed(source, dict).get("n"), 1)
                for key in ("title", "text", "url"):
                    if key in source:
                        typed(source[key], str)
    return events


def native(d):
    artifacts = typed(d.get("artifacts", {}), dict)
    for aid, a in artifacts.items():
        project_id(aid)
        typed(a, dict)
        typed(a.get("title"), str)
        require(a.get("kind") in ("html", "svg", "markdown", "code"))
        versions = typed(a.get("versions"), list)
        require(versions and all(isinstance(v, str) for v in versions))
    projects = typed(d.get("projects", {}), dict)
    for pid, p in projects.items():
        project_id(pid)
        typed(p, dict)
        typed(p.get("title"), str)
        integer(p.get("version", 0))
        for fp, content in typed(p.get("files"), dict).items():
            project_path(fp)
            typed(content, str)
        if p.get("entry") is not None:
            project_path(p["entry"])
        for fp, iid in typed(p.get("assets", {}), dict).items():
            project_path(fp)
            integer(iid, 1)
    history = typed(d.get("history", []), list)
    for h in history:
        require(typed(h, dict).get("role") in ("user", "assistant"))
        h.pop("rag_hits", None)  # exported collection IDs cannot establish local document access
        typed(h.get("text"), str)
        for key in ("raw", "note"):
            if key in h:
                typed(h[key], str)
        for iid in typed(h.get("images", []), list):
            integer(iid, 1)
        attachments(h.get("files", []))
    events = checked_events(d.get("events", []), artifacts)
    turns = []
    for t in typed(d.get("turns", []), list):
        require(isinstance(t, list) and len(t) == 2)
        h, e = (integer(i) for i in t)
        require(h < len(history) and e < len(events) and history[h]["role"] == "user" and events[e]["type"] == "user")
        require(not turns or (h > turns[-1][0] and e > turns[-1][1]))
        turns.append((h, e))
    imgs = {}
    for key, item in typed(d.get("images", {}), dict).items():
        require(re.fullmatch(r"[1-9][0-9]{0,9}", key))
        iid = integer(int(key), 1)
        require(typed(item, dict).get("kind") in ("user", "ref", "gen"))
        raw = base64.b64decode(typed(item.get("png"), str), validate=True)
        try:
            with Image.open(io.BytesIO(raw)) as img:
                require(img.format == "PNG" and img.width * img.height <= 64_000_000)
                img.verify()
        except (OSError, SyntaxError, struct.error, Image.DecompressionBombError) as exc:
            raise ValueError("image importée invalide") from exc
        desc = typed(item.get("desc", ""), str)
        parent = item.get("parent")
        imgs[iid] = ({"kind": item["kind"], "desc": desc, "parent": integer(parent, 1) if parent is not None else None}, raw)
    meta = typed(d.get("meta", {}), dict)
    for key in ("title", "folder"):
        if meta.get(key) is not None:
            typed(meta[key], str)
    meta = {"title": meta.get("title"), "folder": meta.get("folder"), "pinned": meta.get("pinned") is True}
    lang = d.get("lang")
    return dict(history=history, events=events, projects=projects, artifacts=artifacts, turns=turns,
                n=max(integer(d.get("n", 0)), max(imgs, default=0)), meta=meta,
                lang=lang if isinstance(lang, str) and lang in harness.LANG_SETS else None), imgs


def messages(d):
    history, events, turns = [], [], []
    for m in typed(d.get("messages"), list):
        role = typed(m, dict).get("role")
        if role not in ("user", "assistant"):
            continue
        text = m.get("content") or ""
        if isinstance(text, list):
            text = " ".join(typed(part.get("text", ""), str) for part in text if isinstance(part, dict))
        typed(text, str)
        if role == "user":
            turns.append((len(history), len(events)))
            history.append({"role": role, "text": text, "images": [], "raw": text})
        else:
            history.append({"role": role, "text": json.dumps({"action": "reply", "args": {"text": text}}, ensure_ascii=False)})
        events.append({"type": "user" if role == "user" else "reply", "text": text, "ts": time.time()})
    require(events)
    title = typed(d.get("title") or "", str)[:60]
    return dict(history=history, events=events, turns=turns, meta={"title": title, "folder": None, "pinned": False}), {}


def restore(data):
    d = {"messages": data} if isinstance(data, list) else typed(data, dict)
    fields, imgs = native(d) if d.get("format") == "sparky-chat" else messages(d)
    # Reserve a fresh directory atomically so a failed import cannot overwrite or remove an existing conversation.
    os.makedirs(harness.DATA, exist_ok=True)
    while True:
        sid = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
        try:
            os.mkdir(os.path.join(harness.DATA, sid))
            break
        except FileExistsError:
            continue
    s = harness.Session(sid)
    try:
        for k, value in fields.items():
            setattr(s, k, value)
        for iid, (meta, raw) in imgs.items():
            path = os.path.join(s.dir, f"import_{iid}.png")
            with open(path, "wb") as f:
                f.write(raw)
            s.images[iid] = meta | {"path": path}
        s.meta["title"] = s.meta.get("title") or ("Importé : " + s.title())
        s.save()
        return s
    except Exception:
        s.delete()
        raise
