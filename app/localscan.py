"""Finds the models already on this PC (LM Studio, Ollama, Hugging Face cache, Jan, GPT4All,
llama.cpp, other ComfyUI installs, any folder) so they can be used without downloading them again.

Assistants (GGUF) are used in place: Sparky never moves nor deletes them. Image models are only
recognised when their files are the ones of the catalog; they are hard-linked (no extra space on
the same disk) or copied into Sparky's ComfyUI.
"""
import hashlib, json, os, shutil, string, threading, time

import catalog
import config

HOME = os.path.expanduser("~")
LOCAL = os.environ.get("LOCALAPPDATA", os.path.join(HOME, "AppData", "Local"))
ROAMING = os.environ.get("APPDATA", os.path.join(HOME, "AppData", "Roaming"))
OLLAMA = os.environ.get("OLLAMA_MODELS") or os.path.join(HOME, ".ollama", "models")
HF_HUB = os.path.join(os.environ.get("HF_HOME") or os.path.join(HOME, ".cache", "huggingface"), "hub")
SOURCES = [  # (folder, name shown in the UI): searched deeply
    (os.path.join(HOME, ".lmstudio", "models"), "LM Studio"),
    (os.path.join(HOME, ".cache", "lm-studio", "models"), "LM Studio"),
    (HF_HUB, "Hugging Face"),
    (os.path.join(HOME, "jan"), "Jan"),
    (os.path.join(ROAMING, "Jan"), "Jan"),
    (os.path.join(LOCAL, "nomic.ai", "GPT4All"), "GPT4All"),
    (os.path.join(LOCAL, "llama.cpp"), "llama.cpp"),
]
SKIP = {"windows", "$recycle.bin", "system volume information", "program files", "program files (x86)", "programdata",
        "appdata", "node_modules", ".git", "__pycache__", "site-packages", "$windows.~bt", "$winreagent", "recovery",
        "windowsapps", "steamapps", ".cache", ".ollama", ".lmstudio", "onedrivetemp", "msocache", "perflogs"}
BUDGET = 30.0      # seconds for the whole scan
MIN_GB = 0.3       # smaller GGUF files are tokenizers, adapters...
NOT_CHAT = ("mmproj", "embed", "rerank", "whisper", "vocab", "lora", "-mtp", "mtp-", "draft")

state = {"state": "idle", "items": [], "dirs": 0, "secs": 0.0}
_lock = threading.Lock()


def _gguf(path):
    try:
        with open(path, "rb") as f:
            return f.read(4) == b"GGUF"
    except OSError:
        return False


def _source(path):
    p = os.path.normcase(path)
    for d, name in SOURCES:
        if p.startswith(os.path.normcase(d)):
            return name
    if "comfyui" in p:
        return "ComfyUI"
    return os.path.dirname(path)


def _key(path):
    return hashlib.sha1(os.path.normcase(os.path.realpath(path)).encode()).hexdigest()[:12]


def _used_paths():
    """Files Sparky already knows (installed catalog models, local models already added)."""
    out = set()
    for m in catalog.CATALOG:
        for f in m["files"]:
            if f and os.path.exists(f[2]):
                out.add(os.path.normcase(os.path.realpath(f[2])))
    return out


class _Scan:
    def __init__(self):
        self.t0 = time.time()
        self.ggufs = {}    # realpath -> (path, size)
        self.named = {}    # basename (lower) -> path, for the catalog image files
        self.dirs = 0
        self.want = {os.path.basename(f[2]).lower() for m in catalog.CATALOG if m["kind"] == "image" for f in m["files"] if f}
        self.sparky = os.path.normcase(config.ROOT)

    def late(self):
        return time.time() - self.t0 > BUDGET

    def walk(self, root, depth):
        stack = [(root, 0)]
        while stack and not self.late():
            d, lvl = stack.pop()
            if os.path.normcase(d).startswith(self.sparky):
                continue
            try:
                it = os.scandir(d)
            except OSError:
                continue
            self.dirs += 1
            state["dirs"] = self.dirs
            with it:
                for e in it:
                    try:
                        if (hasattr(e, "is_junction") and e.is_junction()) or (e.is_symlink() and e.is_dir()):
                            continue  # "Local Settings", "Application Data"... point to folders seen elsewhere
                        if e.is_dir(follow_symlinks=False):
                            if lvl < depth and e.name.lower() not in SKIP and not e.name.startswith("$"):
                                stack.append((e.path, lvl + 1))
                        elif e.is_file():
                            n = e.name.lower()
                            if n.endswith(".gguf"):
                                size = e.stat().st_size
                                if size >= MIN_GB * 2**30 or catalog.SHARD.search(n) or "mmproj" in n:
                                    self.ggufs.setdefault(os.path.realpath(e.path), (e.path, size))
                            elif n in self.want and n not in self.named:
                                self.named[n] = e.path
                    except OSError:
                        continue


def _llm_item(path, size, source, label=None, mmproj=None):
    stem = os.path.splitext(os.path.basename(path))[0]
    match = next((m["id"] for m in catalog.CATALOG if m["kind"] == "llm" and not m.get("custom")
                  and os.path.basename(m["files"][0][2]).lower() == os.path.basename(path).lower()), None)
    return {"key": _key(path), "kind": "llm", "path": path, "mmproj": mmproj, "size_gb": round(size / 2**30, 1),
            "label": catalog.BY_ID[match]["label"] if match else (label or stem), "source": source, "match": match}


def _ollama(items, used, root):
    man = os.path.join(root, "manifests")
    if not os.path.isdir(man):
        return
    for dirpath, _, files in os.walk(man):
        for tag in files:
            try:
                layers = json.load(open(os.path.join(dirpath, tag), encoding="utf-8")).get("layers", [])
            except (OSError, ValueError):
                continue
            blobs = {l.get("mediaType", "").rsplit(".", 1)[-1]: os.path.join(root, "blobs", l["digest"].replace(":", "-"))
                     for l in layers if l.get("digest")}
            path, proj = blobs.get("model"), blobs.get("projector")
            if not path or not os.path.exists(path) or not _gguf(path):
                continue
            if os.path.normcase(os.path.realpath(path)) in used:
                continue
            parts = os.path.relpath(dirpath, man).split(os.sep)   # registry / namespace / model
            name = parts[-1] if len(parts) < 2 or parts[-2] == "library" else parts[-2] + "/" + parts[-1]
            if catalog.blocked(name, tag) or any(w in name.lower() for w in ("embed", "rerank")):
                continue
            items.append(_llm_item(path, os.path.getsize(path), "Ollama", f"{name}:{tag}",
                                   proj if proj and os.path.exists(proj) else None))


def _local_drive(letter):
    """Fixed or removable disk (a disconnected network drive or an empty DVD drive can block the scan)."""
    import ctypes
    return ctypes.windll.kernel32.GetDriveTypeW(letter + ":\\") in (2, 3)  # DRIVE_REMOVABLE, DRIVE_FIXED


def default_roots():
    """(folder, depth) searched: known apps deeply, the user folder, other drives, then the system drive root."""
    sysroot = os.path.splitdrive(HOME)[0] + "\\"
    roots = [(d, 9) for d, _ in SOURCES] + [(HOME, 8)]
    roots += [(c + ":\\", 6) for c in string.ascii_uppercase
              if os.path.normcase(c + ":\\") != os.path.normcase(sysroot) and _local_drive(c)] if os.name == "nt" else []
    return roots + ([(sysroot, 3)] if os.name == "nt" else [])


def find(roots, ollama=None):
    """Models found under `roots` (and in Ollama's store) that Sparky does not know yet."""
    s, used, items = _Scan(), _used_paths(), []
    _ollama(items, used, ollama or OLLAMA)
    for d, depth in roots:
        if os.path.isdir(d):
            s.walk(d, depth)
    found = sorted(s.ggufs.values(), key=lambda x: x[0].lower())
    sizes = dict(found)
    vision = {}  # folder -> its mmproj (vision) file
    for p, _ in found:
        if "mmproj" in os.path.basename(p).lower():
            vision.setdefault(os.path.dirname(p), p)
    for path, _ in found:  # assistants
        n = os.path.basename(path).lower()
        if any(w in n for w in NOT_CHAT) or catalog.blocked(path):
            continue
        if os.path.normcase(os.path.realpath(path)) in used or not _gguf(path):
            continue
        parts = catalog.shard_paths(path, sizes)
        if not parts or any(not _gguf(p) for p in parts[1:]):
            continue
        item = _llm_item(path, sum(sizes[p] for p in parts), _source(path), mmproj=vision.get(os.path.dirname(path)))
        item["parts"] = parts[1:]
        items.append(item)
    for m in catalog.CATALOG:  # image models of the catalog found complete elsewhere (another ComfyUI...)
        if m["kind"] != "image" or catalog.installed(m):
            continue
        src = [s.named.get(os.path.basename(f[2]).lower()) for f in m["files"] if f]
        if all(src):
            items.append({"key": _key(src[0]), "kind": "image", "match": m["id"], "label": m["label"], "path": src[0],
                          "files": src, "size_gb": round(sum(os.path.getsize(p) for p in src) / 2**30, 1),
                          "source": _source(src[0])})
    state["dirs"] = s.dirs
    seen = set()  # the same file can be reached twice (Hugging Face snapshot + blob)
    return [i for i in items if not (i["key"] in seen or seen.add(i["key"]))]


def run(roots=None):
    with _lock:
        if state["state"] == "running":
            return
        state.update(state="running", items=[], dirs=0, secs=0.0)
    t0 = time.time()
    try:
        state["items"] = find(roots or default_roots())
    finally:
        state.update(state="done", secs=round(time.time() - t0, 1))


def start():
    threading.Thread(target=run, daemon=True).start()


def status():
    used = _used_paths()
    def added(i):
        return (os.path.normcase(os.path.realpath(i["path"])) in used or "local:" + i["key"] in catalog.BY_ID
                or bool(i["match"] and catalog.installed(catalog.BY_ID[i["match"]])))
    return state | {"items": [i | {"added": added(i)} for i in state["items"]]}


def _link(src, dst, copy=True):
    """Hard link (instant, no extra space) when on the same disk; otherwise a copy, or OSError if copy=False."""
    if os.path.exists(dst):
        return
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    try:
        os.link(src, dst)
    except OSError:
        if not copy:
            raise
        shutil.copyfile(src, dst + ".part")
        os.replace(dst + ".part", dst)


def use(key):
    """Adds a found model to Sparky and returns its catalog id."""
    it = next((i for i in state["items"] if i["key"] == key), None)
    if not it:
        raise RuntimeError("modèle introuvable, relance la recherche")
    if it["kind"] == "image":
        m = catalog.BY_ID[it["match"]]
        for src, f in zip(it["files"], [f for f in m["files"] if f]):
            _link(src, f[2])
        return m["id"]
    if it["match"]:   # a catalog model: link it where Sparky expects it (keeps its speed and quality data)
        m = catalog.BY_ID[it["match"]]
        try:
            _link(it["path"], m["files"][0][2], copy=False)
            if it["mmproj"] and m["files"][1]:
                _link(it["mmproj"], m["files"][1][2], copy=False)
            return m["id"]
        except OSError:
            pass  # other disk: used in place below
    mid = "local:" + key
    if mid not in catalog.BY_ID:
        name = it["label"] + " " + os.path.basename(it["path"])
        total, active = catalog._params(name)
        size = it["size_gb"]
        m = {"id": mid, "kind": "llm", "label": it["label"], "quality": catalog.quality_guess(total), "repo": "",
             "desc": it["source"] + " · " + it["path"], "files": [(None, None, it["path"]), (None, None, it["mmproj"]) if it["mmproj"] else None],
             "ref_tps": None, "size_gb": size, "active_gb": size * active / total if active and total else size,
             "think": any(t in name.lower() for t in catalog.THINKERS), "vision": bool(it["mmproj"]),
             "custom": True, "local": True, "qualityGuess": True}
        m["files"].extend((None, None, p) for p in it.get("parts", ()))
        catalog.CATALOG.append(m)
        catalog.BY_ID[mid] = m
        catalog.save_custom()
    return mid
