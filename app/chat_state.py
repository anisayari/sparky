"""Reconstruct only the resources visible at a point in a conversation."""
import copy
import os
import shutil
from pathlib import Path


def rebuild(session, images, masks, source_dir=None):
    artifacts, projects, visible, painted = {}, {}, set(), {}
    for event in session.events:
        kind = event.get("type")
        if kind == "artifact":
            art = artifacts.setdefault(event["id"], {"versions": []})
            art.update(title=event.get("title") or event["id"], kind=event["kind"],
                       language=event.get("language") or event["kind"])
            art["versions"].append(event["content"])
        elif kind == "project":
            proj = projects.setdefault(event["id"], {"files": {}, "assets": {}})
            proj.update(title=event.get("title") or event["id"], entry=event.get("entry"), version=event["version"])
            if "asset" in event:
                proj["assets"][event["path"]] = event["asset"]
                visible.add(event["asset"])
            else:
                proj["files"][event["path"]] = event["content"]
        elif kind == "image":
            visible.add(event["id"])
            visible.update(event.get("refs") or [])
        elif kind == "cresult" and isinstance(event.get("image"), int):
            visible.add(event["image"])
        elif kind == "user":
            visible.update(event.get("images") or [])
            iid = event.get("maskFor")
            name = event.get("maskName")
            if iid and name and isinstance(name, str) and name == os.path.basename(name) and name.startswith(f"mask_{iid}_"):
                candidate = os.path.join(source_dir or session.dir, name)
                if os.path.isfile(candidate):
                    painted[iid] = candidate
            elif iid and iid in masks:
                painted[iid] = masks[iid]
    session.artifacts, session.projects = artifacts, projects
    session.images = {i: copy.deepcopy(images[i]) for i in visible if i in images}
    session.masks = {i: path for i, path in painted.items() if i in visible}
    session._durls.clear()
    session.boxes.clear()


def fork(session, k, after_reply=True):
    if session.ephemeral or type(k) is not int or not 0 <= k < len(session.turns):
        raise ValueError("Message indisponible pour la duplication.")
    h, e = session.turns[k + 1] if k + 1 < len(session.turns) else (len(session.history), len(session.events))
    if not after_reply:
        start_h, start_e = session.turns[k]
        h, e = start_h + 1, start_e + 1
    branch = type(session)()
    try:
        branch.lang = session.lang
        branch.meta["title"] = ("Branche · " + (session.meta.get("title") or session.title()))[:80]
        branch.turns = list(session.turns[:k + 1])
        branch.history = copy.deepcopy(session.history[:h])
        for message in branch.history:
            if message.get("role") == "user":
                message["text"] = session._scoped_history_text(message, set())
            message.pop("rag_hits", None)
        branch.events = copy.deepcopy(session.events[:e])
        for event in branch.events:
            if event.get("type") == "sources" and event.get("kind") == "docs":
                event["items"] = []
            if event.get("type") == "approve":
                event.clear()
                event["type"] = "ignored"
        if after_reply and k == len(session.turns) - 1:
            # An imported transcript can contain resources without corresponding timeline events.
            for key in ("projects", "artifacts", "images", "masks"):
                setattr(branch, key, copy.deepcopy(getattr(session, key)))
        else:
            rebuild(branch, session.images, {}, session.dir)
        for iid, img in branch.images.items():
            path = copy_media(session.dir, img["path"], branch.dir, f"{iid}.png")
            img["path"] = path
        for iid, path in branch.masks.items():
            name = os.path.basename(path)
            branch.masks[iid] = copy_media(session.dir, os.path.join(session.dir, name), branch.dir, name)
        for event in branch.events:
            name, iid = event.get("maskName"), event.get("maskFor")
            if (event.get("type") == "user" and iid and isinstance(name, str) and os.path.basename(name) == name
                    and name.startswith(f"mask_{iid}_") and os.path.isfile(os.path.join(session.dir, name))
                    and not os.path.exists(os.path.join(branch.dir, name))):
                copy_media(session.dir, os.path.join(session.dir, name), branch.dir, name)
        branch.n = max(branch.images, default=0)
        branch.save()
        return branch
    except BaseException:
        branch.delete()
        raise


def copy_media(source_dir, path, destination, name):
    src = Path(path).resolve()
    if src.parent != Path(source_dir).resolve():
        raise ValueError("Ressource hors de la conversation.")
    target = os.path.join(destination, name)
    shutil.copyfile(src, target)
    return target
