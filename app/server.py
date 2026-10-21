"""Sparky: chat UI + agent harness (harness.py) + vision LLM (llama-server) + ComfyUI.

Usage: python app\\server.py [--port 7860] [--no-llm]   (or start.bat)
The assistant model, image model and settings are chosen in the UI (Settings).
Generated projects are served on the next port (preview origin): they cannot reach the API.
"""
import argparse, base64, io, json, os, re, shutil, subprocess, sys, threading, time, urllib.request
import http.server, socketserver
from urllib.parse import parse_qs, unquote, urlparse

import psutil
from PIL import Image

import attach
import catalog
import cloud
import config
import harness
import localscan
import mcp
import openai_api
import rag
import scheduler
import studio
import syscheck
import updater
import workspace

HERE = os.path.dirname(os.path.abspath(__file__))
LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")
NOWIN = config.NOWIN
LLM_LOG = os.path.join(config.DATA, "llm.log")
LLMS = catalog.BY_ID    # all models (catalog + Hugging Face additions), filtered by kind when used
MAX_BODY = 300 * 2**20  # an attached file is at most 200 MB (base64 makes it a third bigger)
NO_ASSISTANT = "Aucun assistant n'est installé : ouvre Paramètres > Bibliothèque de modèles."
SID = re.compile(r"\d{8}-\d{6}-[0-9a-f]{4}")  # Session ids (they are folder names)
sessions = {}           # id -> Session (in memory)
llm_state = {"name": None, "ctx": 0}  # ctx: context window of the running llama-server
failed = set()          # models that just failed to load: not retried automatically (the user can retry)
# model switch in progress, tracked by the UI (spinner + steps)
switch = {"kind": None, "id": None, "label": "", "phase": "idle", "steps": [], "t0": 0.0, "eta": 0, "error": ""}
_switch_lock = threading.Lock()


# ---------------------------------------------------------------- engines (llama-server, ComfyUI)
def alive(url):
    try:
        with urllib.request.urlopen(url, timeout=2):
            return True
    except Exception:
        return False


def port_listening(port):
    import socket
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def stop_llm():
    """Stops llama-server (only it: another program may use the same port)."""
    for c in psutil.net_connections("tcp"):
        if c.laddr and c.laddr.port == config.LLM_PORT and c.status == psutil.CONN_LISTEN and c.pid:
            try:
                p = psutil.Process(c.pid)
                if "llama" in p.name().lower():
                    p.kill()
            except psutil.Error:
                pass
    for _ in range(40):
        if not port_listening(config.LLM_PORT):
            return
        time.sleep(0.25)


def stop_comfy():
    """Stops ComfyUI (only the python process that listens on its port)."""
    for c in psutil.net_connections("tcp"):
        if c.laddr and c.laddr.port == config.COMFY_PORT and c.status == psutil.CONN_LISTEN and c.pid:
            try:
                p = psutil.Process(c.pid)
                if "python" in p.name().lower():
                    p.kill()
            except psutil.Error:
                pass
    for _ in range(40):
        if not port_listening(config.COMFY_PORT):
            return
        time.sleep(0.25)


def restart_comfy():
    stop_comfy()
    start_comfy()


def free_comfy_memory(timeout=10):
    """Unloads ComfyUI's models (VRAM) before loading another big model. ComfyUI answers with an empty body."""
    try:
        harness.http_json(harness.COMFY + "/free", {"unload_models": True, "free_memory": True}, timeout=timeout)
    except Exception:
        pass


def _spawn(cmd, log, **kw):
    os.makedirs(config.DATA, exist_ok=True)
    with open(log, "w") as out:  # the child keeps its own handle
        return subprocess.Popen(cmd, creationflags=NOWIN, stdout=out, stderr=subprocess.STDOUT, **kw)


def start_comfy():
    """Starts ComfyUI (image generation) if it isn't already running."""
    if port_listening(config.COMFY_PORT) or not os.path.exists(os.path.join(config.COMFY_DIR, "main.py")):
        return
    _spawn([sys.executable, "main.py", "--listen", "127.0.0.1", "--port", str(config.COMFY_PORT)],
           os.path.join(config.DATA, "comfy.log"), cwd=config.COMFY_DIR)
    print("ComfyUI en cours de demarrage")


def installed_llms():
    return [k for k, m in LLMS.items() if m["kind"] == "llm" and catalog.installed(m)]


CTX_MIN, CTX_MAX = 4096, 262144


def auto_ctx(m):
    """Largest comfortable context for this model: depends on the memory left once the model is loaded."""
    spare = psutil.virtual_memory().total / 2**30 - m.get("size_gb", 0)
    return 65536 if spare >= 12 else 32768 if spare >= 6 else 16384


def llm_ctx(m):
    """Context window to launch with: the one chosen in Settings, or the automatic one (llm_ctx = 0)."""
    try:
        chosen = int(harness.SETTINGS.get("llm_ctx") or 0)
    except (TypeError, ValueError):
        chosen = 0
    return min(CTX_MAX, max(CTX_MIN, chosen)) if chosen else auto_ctx(m)


def launch_llm(m):
    if not os.path.exists(config.LLAMA_SERVER):
        raise RuntimeError("llama-server introuvable : " + config.LLAMA_SERVER + (" (relance install.ps1)" if os.name == "nt" else " (relance install.sh)"))
    args = catalog.llm_args(m)
    gpu = syscheck.current()["assess"]["mode"] != "cpu"
    ctx = llm_state["ctx"] = llm_ctx(m)
    _spawn([config.LLAMA_SERVER, "-m", args[0], "--port", str(config.LLM_PORT), "-ngl", "99" if gpu else "0",
            "-c", str(ctx), "--jinja", "-fa", "on", "-ctk", "q8_0", "-ctv", "q8_0", *args[1:]], LLM_LOG)
    print("Assistant en cours de demarrage :", m["label"], f"(contexte {ctx})")


def autostart():
    """As soon as a model has just been installed and none is active, activate it automatically."""
    while True:
        time.sleep(4)
        try:
            if switch["phase"] == "loading" or llm_state.get("paused"):
                continue
            ok = [i for i in installed_llms() if i not in failed]
            ok = [i for i in ok if not LLMS[i].get("cloud")] or ok  # a local model first
            if not llm_state["name"] and ok:
                rec = syscheck.current().get("recommended", {}).get("llm")
                use_model(rec if rec in ok else ok[0], auto=True)
            elif not harness.SETTINGS.get("ckpt") or not any(
                    m["kind"] == "image" and m["ckpt"] == harness.SETTINGS["ckpt"] and catalog.installed(m) for m in catalog.CATALOG):
                imgs = [m for m in catalog.CATALOG if m["kind"] == "image" and catalog.installed(m) and m["id"] not in failed]
                if imgs and alive(harness.COMFY + "/system_stats"):  # loaded through the switch: the UI shows it
                    rec = syscheck.current().get("recommended", {}).get("image")
                    use_model(next((x for x in imgs if x["id"] == rec), imgs[0])["id"], auto=True)
        except Exception as e:
            print("autostart :", e)


# ---------------------------------------------------------------- model switch (shown step by step in the UI)
def _step(text, state="run"):
    if switch["steps"] and switch["steps"][-1]["state"] == "run":
        switch["steps"][-1]["state"] = "ok"
    if text:
        switch["steps"].append({"text": text, "state": state, "t": time.time() - switch["t0"]})


def _unload_local():
    old = LLMS.get(llm_state["name"], {})
    if alive(harness.LLM + "/health"):
        _step(f"Décharge {old.get('label') or 'le modèle local'}")
        stop_llm()
    llm_state["name"] = None


def _switch_cloud(mid, m):
    """Runs at the provider: free the GPU, then check the key and the model."""
    _unload_local()
    _step(f"Connexion à {m['providerLabel']}")
    cloud.ping(m)
    llm_state["name"] = harness.SETTINGS["llm"] = mid


def _switch_llm(mid, m):
    _unload_local()
    _step("Libère la mémoire")
    free_comfy_memory()
    switch["eta"] = round(m["size_gb"] * 1.3 + 6)
    launch_llm(m)
    _step(f"Charge {m['label']}")
    t0 = time.time()
    while not alive(harness.LLM + "/health"):
        if time.time() - t0 > 15 and not port_listening(config.LLM_PORT):
            with open(LLM_LOG, encoding="utf-8", errors="replace") as f:
                tail = f.read()[-300:]
            raise RuntimeError("le modèle n'a pas pu se charger (mémoire insuffisante ?)\n" + tail)
        if time.time() - t0 > 600:
            stop_llm()
            raise RuntimeError("chargement trop long")
        time.sleep(0.5)
    llm_state["name"] = harness.SETTINGS["llm"] = mid  # only once it answers


def _switch_image(m):
    _step("Décharge l'ancien modèle d'image")
    free_comfy_memory(15)
    harness.SETTINGS["ckpt"] = m["ckpt"]
    switch["eta"] = 25
    _step(f"Charge {m['label']}")
    if m.get("engine") in harness.INSTRUCT:
        switch["eta"] = 60 if m.get("engine") == "qwen21" else 20
        warm = harness.build_instruct(m, "warmup", 1, size=(256, 256), steps=1)
    else:
        warm = harness.build_graph("warmup", "", 1, size=(256, 256))
        warm["10"]["inputs"]["steps"] = 1
    warm["20"] = {"class_type": "PreviewImage", "inputs": {"images": warm["20"]["inputs"]["images"]}}
    with harness.busy("image"):
        harness._comfy_run(warm)  # tiny first image: forces the checkpoint to load


def _switch(mid):
    m = catalog.BY_ID[mid]
    try:
        if m.get("cloud"):
            _switch_cloud(mid, m)
        elif m["kind"] == "llm":
            _switch_llm(mid, m)
        else:
            _switch_image(m)
        harness.save_settings()
        _step(None)
        switch["phase"] = "ready"
    except Exception as e:
        failed.add(mid)
        if switch["steps"]:
            switch["steps"][-1]["state"] = "err"
        switch.update(phase="error", error=str(e))


def voice_model():
    """Selected dictation model (or the recommended / first installed one), and whether it is ready."""
    voices = [m for m in catalog.CATALOG if m["kind"] == "voice"]
    ok = [m for m in voices if catalog.installed(m)]
    rec = syscheck.current().get("recommended", {}).get("voice")
    pick = next((m for m in ok if m["id"] == harness.SETTINGS.get("voice")), None) or \
        next((m for m in ok if m["id"] == rec), None) or (ok[0] if ok else catalog.BY_ID.get(rec) or voices[0])
    return pick, catalog.installed(pick)


def use_model(mid, auto=False):
    m = catalog.BY_ID.get(mid)
    if not m or not catalog.installed(m):
        return False
    if m["kind"] == "embed":  # Sparky Bot's memory model: always used once installed
        return True
    if m["kind"] in ("voice", "video"):  # nothing to load now: Whisper / the video model load on first use
        harness.SETTINGS[m["kind"]] = mid
        harness.save_settings()
        return True
    with _switch_lock:  # one switch at a time
        if switch["phase"] == "loading":
            return False
        if not auto:
            failed.discard(mid)  # the user asks again: retry
        switch.update(kind=m["kind"], id=mid, label=m["label"], phase="loading", steps=[], t0=time.time(), eta=0, error="")
    threading.Thread(target=_switch, args=(mid,), daemon=True).start()
    return True


# ---------------------------------------------------------------- sessions, metrics
def get_session(sid):
    """Session in memory or on disk; None for an unknown or malformed id (ids become folder names)."""
    sid = str(sid or "")
    if sid in sessions:
        return sessions[sid]
    if not SID.fullmatch(sid) or not os.path.exists(os.path.join(harness.DATA, sid, "session.json")):
        return None
    sessions[sid] = harness.Session.load(sid)
    return sessions[sid]


_ckpts = {"t": 0.0, "v": []}


def checkpoints():
    """ComfyUI checkpoints (cached 30 s: asked by every /api/settings, ComfyUI can be slow to answer)."""
    if time.time() - _ckpts["t"] > 30:
        try:
            info = harness.http_json(harness.COMFY + "/object_info/CheckpointLoaderSimple", timeout=5)
            _ckpts["v"] = info["CheckpointLoaderSimple"]["input"]["required"]["ckpt_name"][0]
        except Exception:
            _ckpts["v"] = []
        _ckpts["t"] = time.time()
    return _ckpts["v"]


def num(v):
    try:
        return float(v)
    except ValueError:
        return None


_gpu = {"t": 0.0, "v": {}, "missing": False}
_gpu_lock = threading.Lock()


def gpu_stats():
    """nvidia-smi is slow (~100 ms): read at most every 5 s, by one thread, and never again once it is missing."""
    with _gpu_lock:
        if not _gpu["missing"] and time.time() - _gpu["t"] > 5:
            _gpu["t"] = time.time()
            try:
                line = subprocess.run(["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw",
                                       "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=5,
                                      creationflags=NOWIN).stdout.splitlines()[0]
                u, mu, mt, tp, pw = [num(x.strip()) for x in line.split(",")]
                _gpu["v"] = {"util": u, "used": mu, "total": mt, "temp": tp, "power": pw}
            except FileNotFoundError:
                _gpu.update(missing=True, v={})
            except Exception:
                _gpu["v"] = {}
        return _gpu["v"]


def metrics():
    vm = psutil.virtual_memory()
    cur = LLMS.get(llm_state["name"] or llm_state.get("paused") or "", {})
    llm_up = bool(cur.get("cloud")) or alive(harness.LLM + "/health")
    try:
        q = harness.http_json(harness.COMFY + "/queue", timeout=2)
        comfy = {"up": True, "running": len(q["queue_running"]), "pending": len(q["queue_pending"])}
    except Exception:
        comfy = {"up": False, "running": 0, "pending": 0}
    a = syscheck.current()
    return {"cpu": psutil.cpu_percent(), "ram": {"used": vm.used / 2**30, "total": vm.total / 2**30},
            "gpu": gpu_stats(), "gpuName": a["assess"].get("gpu"), "cores": a["hw"].get("cores"),
            "unified": a["assess"]["mode"] == "unified",
            "llm": {"name": cur.get("label"), "up": llm_up, "paused": bool(llm_state.get("paused")), "active": harness.ACTIVITY["llm"] > 0,
                    "cloud": cur.get("providerLabel") if cur.get("cloud") else None},
            "image": {"name": harness.image_model().get("label") if harness.SETTINGS.get("ckpt") else None, "up": comfy["up"],
                      "active": harness.ACTIVITY["image"] > 0 or comfy["running"] > 0, "queue": comfy["pending"]},
            "switch": {"phase": switch["phase"], "kind": switch["kind"], "label": switch["label"]}}


def run_project(s, pid, p):
    """Run button: same engine as the automatic tests (Python, Node.js with npm, website in a headless browser)."""
    import checker
    r = checker.check(s, pid, p, config.PREVIEW_PORT)
    return {"code": r["code"], "output": r["output"] or "(aucune sortie)", "secs": r["secs"], "ok": r["ok"], "kind": r["kind"]}


def wait_llm(emit):
    """The assistant is paused for a Studio video or reloading after it: wait (with a status) instead of failing."""
    t0 = time.time()
    while (llm_state.get("paused") or (switch["phase"] == "loading" and switch["kind"] == "llm")) and time.time() - t0 < 900:
        emit({"type": "status", "text": "l'assistant est en pause pendant une vidéo du Studio, il revient juste après"
              if llm_state.get("paused") else "rechargement de l'assistant"})
        time.sleep(3)
    while switch["phase"] == "loading" and time.time() - t0 < 900:
        time.sleep(1)


def apply_config(s, new):
    if "assistant" in new:
        s.config["assistant"] = new["assistant"] or None
    if isinstance(new.get("system"), str):
        s.config["system"] = new["system"][:8000]
    if "docs" in new:
        s.config["docs"] = [int(x) for x in new["docs"] or []]
    if "web" in new:
        s.config["web"] = bool(new["web"])
    import computer
    if new.get("computer") in computer.LEVELS and new["computer"] != s.config.get("computer"):
        s.config["computer"] = new["computer"]
        s.config["computer_allow"] = []  # "always allow" choices do not survive a change of level


def run_scheduled(prompt, title):
    """Scheduled task: runs the prompt in a new conversation, returns its id."""
    s = harness.Session()
    s.meta["title"] = "⏰ " + title
    sessions[s.id] = s
    s.config["web"] = True  # tasks like "summarize today's news" need the web
    s.turn(prompt, [], None, None, lambda e: None)
    return s.id


# ---------------------------------------------------------------- Studio: the assistant makes room for video models
def pause_llm_for_video(m):
    """A video model (20-40 GB) does not fit next to a local assistant: stop it during the video (x5 faster)."""
    cur = LLMS.get(llm_state["name"] or "")
    if not cur or cur.get("cloud") or llm_state.get("paused"):
        return
    mem = syscheck.current()["assess"].get("model_mem_gb", 0)
    if cur["size_gb"] + m["size_gb"] + 4 <= mem:
        return  # big machine: both fit
    llm_state["paused"] = llm_state["name"]
    llm_state["name"] = None
    stop_llm()
    print("Assistant en pause pendant la video")


def resume_llm_after_video():
    prev = llm_state.get("paused")
    if not prev or any(j["state"] in ("queued", "running") for j in studio.jobs):
        return
    free_comfy_memory(timeout=30)  # unload the video model first
    llm_state["paused"] = None
    use_model(prev, auto=True)


def decode_image(data_url):
    return Image.open(io.BytesIO(base64.b64decode(data_url.split(",", 1)[1])))


LIMITS = {"steps": (1, 150, int), "cfg": (0.0, 30.0, float), "max_steps": (1, 20, int)}


def update_settings(new):
    """Settings sent by the UI, checked and clamped (an empty field must not stop the agent)."""
    for k, (lo, hi, kind) in LIMITS.items():
        try:
            if k in new:
                harness.SETTINGS[k] = min(hi, max(lo, kind(new[k])))
        except (TypeError, ValueError):
            pass
    if new.get("think") in harness.THINK:
        harness.SETTINGS["think"] = new["think"]
    if "autotest" in new:
        harness.SETTINGS["autotest"] = bool(new["autotest"])
    if new.get("ui_lang") in ("en", "fr"):
        harness.SETTINGS["ui_lang"] = new["ui_lang"]
    for k in ("custom", "searx"):
        if isinstance(new.get(k), str):
            harness.SETTINGS[k] = new[k][:8000].strip()
    if "mcp_on" in new:
        harness.SETTINGS["mcp_on"] = bool(new["mcp_on"])
    if "llm_ctx" in new:  # 0 = automatic; takes effect at the next (re)start of the assistant
        try:
            n = int(new["llm_ctx"] or 0)
            harness.SETTINGS["llm_ctx"] = min(CTX_MAX, max(CTX_MIN, n)) if n else 0
        except (TypeError, ValueError):
            pass
    harness.save_settings()


# ---------------------------------------------------------------- HTTP API
class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="application/json", headers=()):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def trusted(self):
        """Only the Sparky page may drive the API: a foreign Host is a DNS-rebinding attempt, a foreign Origin
        (another website, or a generated project on the preview port) is a cross-site request."""
        if urlparse("//" + (self.headers.get("Host") or "")).hostname not in LOCAL_HOSTS:
            return False
        if self.path.startswith("/v1/"):  # OpenAI-compatible API: protected by its bearer key (openai_api.py)
            return True
        if self.command != "POST":
            return True
        origin = self.headers.get("Origin")
        if origin is not None:
            o = urlparse(origin)
            if o.hostname not in LOCAL_HOSTS or (o.port or 80) != self.server.server_address[1]:
                return False
        return self.headers.get("Sec-Fetch-Site") != "cross-site"

    def raw_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            raise OverflowError
        return self.rfile.read(n)

    def body(self):
        return json.loads(self.raw_body() or b"{}")

    def query(self):
        return {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}

    def stream(self):
        """Opens a Server-Sent Events response and returns an emit(event) function."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        self.streaming = True

        def emit(e):
            try:
                self.wfile.write(("data: " + json.dumps(e, ensure_ascii=False) + "\n\n").encode())
                self.wfile.flush()
            except OSError:
                pass  # the tab was closed: the turn continues and stays in the history
        self.emit = emit
        return emit

    def dispatch(self, routes, prefixes):
        """One place for routing and errors: a failing request gets a JSON error, never a dropped connection."""
        self.streaming = False
        if not self.trusted():
            return self.send(403, {"error": "requête refusée"})
        path = self.path.split("?")[0]
        if path.startswith("/v1/") and self.server.server_address[1] == config.APP_PORT:
            return openai_api.handle(self, path)
        fn = routes.get(path) or next((f for p, f in prefixes if path.startswith(p)), None)
        if not fn:
            return self.send(404, {"error": "introuvable"})
        try:
            fn(self, path)
        except OverflowError:
            self.send(413, {"error": "fichier trop gros"})
        except Exception as e:
            print("erreur", self.command, path, ":", repr(e))
            if self.streaming:
                self.emit({"type": "error", "text": f"erreur : {e}"})
                self.emit({"type": "done"})
            else:
                try:
                    self.send(500, {"error": str(e)})
                except OSError:
                    pass

    def do_GET(self):
        self.dispatch(GET, GET_PREFIX)

    def do_POST(self):
        self.dispatch(POST, POST_PREFIX)

    # -- GET
    def page(self, path):
        with open(os.path.join(HERE, "index.html"), "rb") as f:
            self.send(200, f.read(), "text/html; charset=utf-8")

    def vendor(self, path):  # Markdown libraries (tables, LaTeX, Mermaid, code colors), served locally
        import mimetypes
        name = path[len("/vendor/"):]
        p = os.path.join(HERE, "vendor", *name.split("/"))
        if not re.fullmatch(r"(fonts/)?[\w][\w.-]*", name) or not os.path.isfile(p):
            return self.send(404, {"error": "introuvable"})
        with open(p, "rb") as f:
            self.send(200, f.read(), (mimetypes.guess_type(name)[0] or "application/octet-stream")
                      + ("; charset=utf-8" if name.endswith((".js", ".css")) else ""), [("Cache-Control", "max-age=86400")])

    def logo(self, path):
        with open(os.path.join(HERE, "logo.png"), "rb") as f:
            self.send(200, f.read(), "image/png")

    def health(self, path):
        self.send(200, {"comfy": alive(harness.COMFY + "/system_stats"), "llm": alive(harness.LLM + "/health"),
                        "llmName": LLMS.get(llm_state["name"], {}).get("label")})

    def state(self, path):
        s = get_session(self.query().get("id"))
        self.send(200, {"id": s.id, "events": s.events, "turns": len(s.turns), "versions": s.versions(), "config": s.config,
                        "meta": s.meta, "busy": s.lock.locked()} if s else {"id": None, "events": []})

    def image(self, path):  # /api/img/<session>/<n>
        try:
            sid, n = path[len("/api/img/"):].rsplit("/", 1)
            with open(get_session(sid).images[int(n)]["path"], "rb") as f:
                self.send(200, f.read(), "image/png")
        except (AttributeError, KeyError, ValueError, OSError):
            self.send(404, {"error": "image inconnue"})

    def projzip(self, path):  # whole project as a ZIP
        import zipfile
        q = self.query()
        s = get_session(q.get("id"))
        p = (s.projects if s else {}).get(q.get("project"))
        if not p:
            return self.send(404, {"error": "projet inconnu"})
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for fp, content in p["files"].items():
                z.writestr(f"{q['project']}/{fp}", content)
            for fp in p.get("assets", {}):
                src = s.asset_file(q["project"], fp)
                if src and os.path.exists(src):
                    z.write(src, f"{q['project']}/{fp}")
        name = re.sub(r"[^\w.-]+", "_", q["project"])
        self.send(200, buf.getvalue(), "application/zip", [("Content-Disposition", f'attachment; filename="{name}.zip"')])

    def settings(self, path):
        known = {m["ckpt"] for m in catalog.CATALOG if m["kind"] == "image"}
        others = [c for c in checkpoints() if c not in known and not catalog.blocked(c)]
        self.send(200, {"settings": harness.SETTINGS, "catalog": catalog.listing(harness.SETTINGS),
                        "otherCheckpoints": others, "active": {"llm": llm_state["name"], "voice": voice_model()[0]["id"], "video": (studio.video_model() or {}).get("id"), "ctx": llm_state["ctx"],
                                   "autoCtx": auto_ctx(catalog.BY_ID.get(llm_state["name"]) or {})},
                        "syscheck": syscheck.current(), "onboarded": bool(harness.SETTINGS.get("onboarded"))})

    def search_chats(self, path):  # full-text search in every conversation
        q = (self.query().get("q") or "").strip().lower()
        out = []
        for row in harness.Session.list_all() if q else []:
            try:
                st = json.load(open(os.path.join(harness.DATA, row["id"], "session.json"), encoding="utf-8"))
            except (OSError, ValueError):
                continue
            hit = row["title"] if q in row["title"].lower() else None
            for e in st.get("events", []):
                if hit:
                    break
                t = e.get("text") or ""
                if e.get("type") in ("user", "reply") and q in t.lower():
                    i = t.lower().index(q)
                    hit = ("…" if i > 40 else "") + t[max(0, i - 40):i + 80].replace("\n", " ")
            if hit:
                out.append(row | {"snippet": hit})
            if len(out) >= 40:
                break
        self.send(200, out)

    def export(self, path):  # one conversation as JSON (with its images) or Markdown
        q = self.query()
        s = get_session(q.get("id"))
        if not s:
            return self.send(404, {"error": "conversation inconnue"})
        title = s.meta.get("title") or s.title()
        name = re.sub(r"[^\w-]+", "_", title)[:50] or "conversation"
        if q.get("fmt") == "md":
            lines = [f"# {title}\n"]
            for e in s.events:
                if e.get("type") == "user" and not e.get("hidden"):
                    lines.append(f"## 🧑 {e.get('text') or ''}\n" + "".join(f"\n![image {i}](image-{i}.png)" for i in e.get("images") or []))
                elif e.get("type") == "reply":
                    lines.append(f"**Sparky** :\n\n{e['text']}\n")
                elif e.get("type") == "image":
                    lines.append(f"![image {e['id']}](image-{e['id']}.png)\n")
            return self.send(200, "\n".join(lines).encode(), "text/markdown; charset=utf-8",
                             [("Content-Disposition", f'attachment; filename="{name}.md"')])
        imgs = {}
        for i, v in s.images.items():
            try:
                imgs[i] = base64.b64encode(open(v["path"], "rb").read()).decode()
            except OSError:
                pass
        st = {k: getattr(s, k) for k in ("history", "events", "n", "lang", "artifacts", "projects", "config", "meta", "turns")}
        st |= {"format": "sparky-chat", "version": 1, "title": title,
               "images": {i: {k: v for k, v in s.images[i].items() if k != "path"} | {"png": b} for i, b in imgs.items()}}
        self.send(200, json.dumps(st, ensure_ascii=False).encode(), "application/json",
                  [("Content-Disposition", f'attachment; filename="{name}.json"')])

    def workspace_get(self, path):
        d = workspace.get()
        self.send(200, d | {"knowledge": rag.collections(), "mcp": {"config": mcp.load(), "status": mcp.status()},
                            "api": openai_api.settings() | {"url": f"http://127.0.0.1:{config.APP_PORT}/v1",
                                                             "model": llm_state["name"]},
                            "schedules": scheduler.list_(), "unread": scheduler.unread(),
                            "settings": {k: harness.SETTINGS.get(k) for k in ("custom", "searx", "mcp_on")}})

    def studio_lib(self, path):
        q = self.query()
        vm = studio.video_model()
        vids = [m for m in catalog.CATALOG if m["kind"] == "video"]
        self.send(200, {"items": studio.library(q.get("kind", "all")), "jobs": studio.status(),
                        "video": {"id": vm["id"], "label": vm["label"], "audio": vm.get("audio", False)} if vm else None,
                        "videoModels": [{"id": m["id"], "label": m["label"], "installed": catalog.installed(m), "size_gb": m["size_gb"],
                                         "audio": m.get("audio", False), "job": catalog.jobs.get(m["id"])} for m in vids],
                        "image": harness.image_model().get("label") if harness.SETTINGS.get("ckpt") else None,
                        "paused": bool(llm_state.get("paused"))})

    def studio_file(self, path):
        p = studio.file_path(path[len("/api/studio/file/"):])
        if not p:
            return self.send(404, {"error": "introuvable"})
        ctype = "video/mp4" if p.endswith(".mp4") else "video/webm" if p.endswith(".webm") else "image/png"
        size = os.path.getsize(p)
        rng = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range") or "")
        with open(p, "rb") as f:
            if rng and ctype.startswith("video"):  # seeking in the <video> player
                a = int(rng[1] or 0)
                b = min(size - 1, int(rng[2]) if rng[2] else size - 1)
                f.seek(a)
                data = f.read(b - a + 1)
                self.send_response(206)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Range", f"bytes {a}-{b}/{size}")
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            self.send(200, f.read(), ctype, [("Accept-Ranges", "bytes"), ("Cache-Control", "max-age=86400")])

    def tts_status(self, path):
        import tts
        self.send(200, tts.status())

    def bot_state(self, path):
        import companion
        e5 = catalog.BY_ID["e5-small"]
        if not catalog.installed(e5) and catalog.jobs.get("e5-small", {}).get("state") != "running":
            catalog.install("e5-small")  # memory search model (0.5 GB), downloaded on first use
        self.send(200, companion.snapshot() | {"embedJob": catalog.jobs.get("e5-small")})

    def voice(self, path):
        m, ready = voice_model()
        self.send(200, {"id": m["id"], "label": m["label"], "size_gb": m["size_gb"], "ready": ready, "job": catalog.jobs.get(m["id"])})

    def attach_status(self, path):
        st = attach.status(self.query().get("id", ""))
        self.send(200 if st else 404, st or {"error": "fichier inconnu"})

    def cloud_models(self, path):
        try:
            self.send(200, cloud.models(self.query().get("provider", "")))
        except Exception as e:
            self.send(400, {"error": str(e)})

    def switch_state(self, path):
        self.send(200, switch | {"elapsed": round(time.time() - switch["t0"], 1) if switch["t0"] else 0})

    def hf(self, path):
        q = self.query()
        try:
            if path.endswith("search"):
                return self.send(200, catalog.hf_search(q.get("q", ""), q.get("kind", "llm")))
            self.send(200, catalog.hf_files(q.get("repo", ""), q.get("kind", "llm")))
        except Exception as e:
            self.send(502, {"error": str(e)})

    # -- POST
    def transcribe(self, path):  # body = raw WAV recorded by the UI
        data = self.raw_body()
        m, ready = voice_model()
        if not ready:
            return self.send(409, {"error": "dictée vocale non installée", "id": m["id"]})
        try:
            import stt
            t0 = time.time()
            text = stt.transcribe(data, m["id"], self.query().get("lang") or None)
            harness.record_speed("img:" + m["id"], (time.time() - t0) * 10 / max(1.0, len(data) / 32000))
            self.send(200, {"text": text})
        except Exception as e:
            self.send(500, {"error": f"transcription impossible : {e}"})

    def import_chat(self, path):  # a conversation exported by Sparky (JSON), or a plain [{role, content}] list
        b = self.body()
        d = b.get("data")
        s = harness.Session()
        if isinstance(d, list):
            d = {"messages": d}
        if not isinstance(d, dict):
            return self.send(400, {"error": "fichier non reconnu"})
        if d.get("format") == "sparky-chat":
            os.makedirs(s.dir, exist_ok=True)
            for i, v in (d.get("images") or {}).items():
                p = os.path.join(s.dir, f"import_{int(i)}.png")
                with open(p, "wb") as f:
                    f.write(base64.b64decode(v.pop("png", "")))
                s.images[int(i)] = v | {"path": p}
            for k in ("history", "events", "n", "lang", "artifacts", "projects", "config", "meta"):
                if k in d:
                    setattr(s, k, d[k] if k not in ("config", "meta") else getattr(s, k) | d[k])
            s.turns = [tuple(x) for x in d.get("turns", [])]
        else:
            msgs = d.get("messages") or []
            for m in msgs:
                role, text = m.get("role"), m.get("content")
                if isinstance(text, list):
                    text = " ".join(x.get("text", "") for x in text if isinstance(x, dict))
                if role == "user":
                    s.turns.append((len(s.history), len(s.events)))
                    s.history.append({"role": "user", "text": text or "", "images": [], "raw": text or ""})
                    s.events.append({"type": "user", "text": text or "", "ts": time.time()})
                elif role == "assistant":
                    s.history.append({"role": "assistant", "text": json.dumps({"action": "reply", "args": {"text": text}}, ensure_ascii=False)})
                    s.events.append({"type": "reply", "text": text or "", "ts": time.time()})
            if not s.events:
                return self.send(400, {"error": "aucun message trouvé"})
            s.meta["title"] = (d.get("title") or "")[:60] or None
        s.meta["title"] = s.meta.get("title") or ("Importé : " + s.title())
        sessions[s.id] = s
        s.save()
        self.send(200, {"id": s.id})

    def chat_meta(self, path):  # rename, pin, folder
        b = self.body()
        s = get_session(b.get("id"))
        if not s:
            return self.send(404, {"error": "conversation inconnue"})
        for k in ("title", "folder"):
            if k in b:
                s.meta[k] = (str(b[k]).strip()[:80] or None) if b[k] is not None else None
        if "pinned" in b:
            s.meta["pinned"] = bool(b["pinned"])
        s.save()
        self.send(200, {"ok": True})

    def chat_config(self, path):  # assistant, own instructions, knowledge, web access of one conversation
        b = self.body()
        s = get_session(b.get("id"))
        if not s:
            return self.send(404, {"error": "conversation inconnue"})
        apply_config(s, b.get("config") or {})
        s.save()
        self.send(200, {"ok": True, "config": s.config})

    def version(self, path):  # shows another version of a message (after edit / regenerate)
        b = self.body()
        s = get_session(b.get("id"))
        if not s or s.lock.locked():
            return self.send(409, {"error": "conversation occupée"})
        with s.lock:
            s.switch_version(int(b["k"]), int(b["j"]))
        self.send(200, {"ok": True})

    def redo(self, path):
        """Edit a message (text) or regenerate an answer (no text) or continue the last answer: streamed like /api/chat."""
        b = self.body()
        s = get_session(b.get("id"))
        if not s:
            return self.send(404, {"error": "conversation inconnue"})
        emit = self.stream()
        emit({"type": "ack", "id": s.id, "busy": s.lock.locked()})
        if not llm_state["name"]:
            emit({"type": "error", "text": NO_ASSISTANT})
            emit({"type": "done"})
            return
        if b.get("mode") == "continue":
            return s.turn("", [], None, None, emit, kind="continue")
        k = int(b.get("k", len(s.turns) - 1))
        s.turn(b.get("text") if b.get("mode") == "edit" else None, [], None, None, emit, rewind=k)

    def workspace_save(self, path):
        """Assistants, prompts, knowledge, MCP servers, API, scheduled tasks: {what, action, ...}."""
        b = self.body()
        what, act = b.get("what"), b.get("action", "save")
        try:
            if what in ("assistants", "prompts"):
                if act == "delete":
                    workspace.delete(what, b.get("id"))
                    return self.send(200, {"ok": True})
                return self.send(200, {"id": workspace.save(what, b.get("item") or {})})
            if what == "collection":
                if act == "delete":
                    rag.delete_collection(int(b["id"]))
                elif act == "rename":
                    rag.rename_collection(int(b["id"]), b.get("name", ""))
                else:
                    return self.send(200, {"id": rag.create_collection(b.get("name") or "Documents")})
                return self.send(200, {"ok": True})
            if what == "document":
                if act == "delete":
                    rag.delete_document(int(b["id"]))
                    return self.send(200, {"ok": True})
                data = base64.b64decode((b.get("data") or "").split(",")[-1])
                return self.send(200, {"id": rag.add_document(int(b["collection"]), b.get("name") or "document", data, b.get("type", ""))})
            if what == "mcp":
                if act == "restart":
                    mcp.restart(b.get("name"))
                else:
                    cfg = b.get("config")
                    if isinstance(cfg, str):
                        cfg = json.loads(cfg or "{}")
                    if not isinstance(cfg, dict):
                        raise ValueError("configuration invalide")
                    mcp.save(cfg if "mcpServers" in cfg else {"mcpServers": cfg})
                    threading.Thread(target=mcp.start_all, daemon=True).start()
                return self.send(200, {"ok": True})
            if what == "api":
                if act == "key":
                    openai_api.regenerate_key()
                else:
                    openai_api.set_enabled(bool(b.get("enabled")))
                return self.send(200, openai_api.settings())
            if what == "schedule":
                if act == "delete":
                    scheduler.delete(b.get("id"))
                elif act == "run":
                    scheduler.run_now(b.get("id"))
                elif act == "read":
                    scheduler.mark_read(b.get("id"))
                else:
                    return self.send(200, {"id": scheduler.save_task(b.get("item") or {})["id"]})
                return self.send(200, {"ok": True})
        except (ValueError, KeyError, TypeError) as e:
            return self.send(400, {"error": str(e)})
        self.send(404, {"error": "introuvable"})

    def tts_speak(self, path):
        import tts
        b = self.body()
        st = tts.status()
        if not st.get("installed"):
            return self.send(409, {"error": "lecture vocale non installée"})
        text = b.get("text", "")[:20000]
        vid = tts.pick_voice(tts.norm_lang(b.get("lang")) or tts.guess_lang(text), b.get("voice"))
        if not tts.ready(vid):  # first use of this language: its voice (~60 MB) is downloaded
            tts.install(vid)
            return self.send(409, {"error": "voix en cours de téléchargement", "voice": vid, "job": tts.jobs.get(vid)})
        try:
            wav = tts.speak(text, None, vid, float(b.get("speed") or 1.0))
        except Exception as e:
            return self.send(409, {"error": str(e)})
        self.send(200, wav, "audio/wav")

    def studio_post(self, path):
        b = self.body()
        act = path.rsplit("/", 1)[-1]
        try:
            if act == "generate":
                return self.send(200, studio.submit(b.get("mode", "image"), b.get("prompt", ""), b.get("aspect", "16:9"),
                                                    b.get("seconds", 5), b.get("source"), b.get("enhance", True)))
            if act == "delete":
                return self.send(200, {"ok": studio.delete(b.get("id"))})
            if act == "cancel":
                j = next((j for j in studio.jobs if j["id"] == b.get("id") and j["state"] == "queued"), None)
                if j:
                    j.update(state="error", error="annulé", ended=time.time())
                return self.send(200, {"ok": bool(j)})
        except ValueError as e:
            return self.send(400, {"error": str(e)})
        self.send(404, {"error": "introuvable"})

    def approve(self, path):  # computer mode: the user's answer to an approval card
        b = self.body()
        s = get_session(b.get("id"))
        ok = bool(s) and s.approve(str(b.get("aid")), b.get("decision"))
        self.send(200 if ok else 404, {"ok": ok} if ok else {"error": "demande expirée"})

    def tts_install(self, path):
        import tts
        tts.install(self.body().get("voice"))
        self.send(200, {"ok": True})

    def new_session(self, path):
        s = harness.Session(ephemeral=bool(self.body().get("ephemeral")))
        # a previous ephemeral conversation disappears as soon as another one is opened
        for k in [k for k, v in sessions.items() if v.ephemeral]:
            sessions.pop(k).delete()
        sessions[s.id] = s
        self.send(200, {"id": s.id, "ephemeral": s.ephemeral})

    def delete_session(self, path):
        sid = self.body().get("id")
        s = get_session(sid)
        sessions.pop(sid, None)
        if s:
            s.delete()
        self.send(200, {"ok": True})

    def install(self, path):
        b = self.body()
        for mid in b.get("ids") or [b.get("id")]:
            catalog.install(mid)
        self.send(200, {"ok": True})

    def rescan(self, path):
        self.send(200, syscheck.run())

    def projrun(self, path):  # runs a project, only when the user clicks Run
        b = self.body()
        s = get_session(b.get("id"))
        p = (s.projects if s else {}).get(b.get("project"))
        if not p:
            return self.send(404, {"error": "projet inconnu"})
        self.send(200, run_project(s, b["project"], p))

    def onboarded(self, path):
        harness.SETTINGS["onboarded"] = True
        harness.save_settings()
        self.send(200, {"ok": True})

    def use(self, path):
        self.send(200, {"ok": use_model(self.body().get("id"))})

    def restart(self, path):
        """{service: "llm" | "comfy", ctx?: tokens}: restarts a service (the assistant reloads with the saved context)."""
        b = self.body()
        if b.get("service") == "comfy":
            threading.Thread(target=restart_comfy, daemon=True).start()
            return self.send(200, {"ok": True})
        if b.get("service") != "llm":
            return self.send(400, {"ok": False, "error": "service inconnu"})
        if "ctx" in b:
            update_settings({"llm_ctx": b["ctx"]})
        mid = llm_state["name"] or harness.SETTINGS.get("llm")
        m = catalog.BY_ID.get(mid)
        if not m or m.get("cloud"):
            return self.send(200, {"ok": False, "error": "Aucun assistant local à redémarrer."})
        self.send(200, {"ok": use_model(mid), "label": m["label"]})  # same model: unload, then load with the new context

    def attach_add(self, path):  # {name, type, data: base64}: text read in the background
        b = self.body()
        self.send(200, attach.add(b.get("name", "fichier"), b.get("data", ""), b.get("type", "")))

    def cloud_key(self, path):
        b = self.body()
        try:
            cloud.set_key(b.get("provider", ""), b.get("key"), b.get("base"))
        except Exception as e:
            return self.send(400, {"error": str(e)})
        self.send(200, {"ok": True})

    def cloud_remove(self, path):
        b = self.body()
        if LLMS.get(llm_state["name"], {}).get("provider") == b.get("provider"):
            return self.send(409, {"error": "modèle en cours d'utilisation : choisis d'abord un autre assistant"})
        cloud.remove(b.get("provider", ""))
        self.send(200, {"ok": True})

    def cloud_add(self, path):  # registers a cloud model in the catalog, then the UI activates it
        b = self.body()
        if b.get("provider") not in cloud.PROVIDERS or not b.get("model"):
            return self.send(400, {"error": "fournisseur ou modèle manquant"})
        m = cloud.entry(b["provider"], b["model"], b.get("label"), b.get("vision"), b.get("think"), b.get("effort"))
        if m["id"] not in catalog.BY_ID:
            catalog.CATALOG.append(m)
            catalog.BY_ID[m["id"]] = m
            catalog.save_custom()
        self.send(200, {"id": m["id"], "label": m["label"]})

    def localscan_start(self, path):
        localscan.start()
        self.send(200, {"ok": True})

    def localscan_use(self, path):
        try:
            mid = localscan.use(self.body().get("key"))
        except Exception as e:
            return self.send(400, {"error": str(e)})
        m = catalog.BY_ID[mid]
        self.send(200, {"id": mid, "label": m["label"], "kind": m["kind"], "ok": use_model(mid)})

    def version_check(self, path):
        updater.check(force=True)
        self.send(200, updater.status())

    def update(self, path):
        ok, msg = updater.update()
        self.send(200 if ok else 409, {"ok": ok, "message": msg})

    def hf_install(self, path):
        b = self.body()
        try:
            self.send(200, {"id": catalog.hf_install(b["repo"], b["path"], b.get("kind", "llm"))})
        except Exception as e:
            self.send(502, {"error": str(e)})

    def model_delete(self, path):
        mid = self.body().get("id")
        ck = catalog.BY_ID.get(mid, {}).get("ckpt")
        if mid == llm_state["name"] or (ck and ck == harness.SETTINGS.get("ckpt")):
            return self.send(409, {"error": "modèle en cours d'utilisation"})
        catalog.delete(mid)
        self.send(200, {"ok": True})

    def save_settings(self, path):
        update_settings(self.body().get("settings") or {})
        self.send(200, {"ok": True})

    def chat(self, path):
        b = self.body()
        s = get_session(b.get("id"))
        if s is None:  # unknown id (e.g. after a restart): a private chat must stay private
            s = harness.Session(ephemeral=bool(b.get("ephemeral")))
            sessions[s.id] = s
        if isinstance(b.get("config"), dict):  # setup chosen before the first message (assistant, web, documents)
            apply_config(s, b["config"])
        emit = self.stream()
        emit({"type": "ack", "id": s.id, "busy": s.lock.locked()})  # received by the server
        wait_llm(emit)
        if not llm_state["name"]:
            emit({"type": "error", "text": NO_ASSISTANT})
            emit({"type": "done"})
            return
        uploads = [decode_image(u).convert("RGB") for u in b.get("images", [])]
        mask = decode_image(b["mask"]).convert("RGB") if b.get("mask") else None
        s.turn(b.get("text", ""), uploads, mask, b.get("maskFor"), emit, files=attach.take(b.get("files")))

    def bot(self, path):
        """Sparky Bot: companion with long-term memory (see companion.py)."""
        import companion
        action, b, S = path[len("/api/bot/"):], self.body(), companion.S
        if action in ("chat", "greet"):
            if not llm_state["name"]:
                return self.send(409, {"error": NO_ASSISTANT})
            emit = self.stream()
            try:
                if action == "chat":
                    companion.chat((b.get("text") or "").strip(), emit)
                else:
                    t = companion.greeting(emit)
                    emit({"type": "reply", "text": t, "bond": companion.bond()} if t else {"type": "noop"})
            except Exception as e:
                emit({"type": "error", "text": f"assistant indisponible : {e}"})
            emit({"type": "done"})
            return
        if action == "memory":  # add / edit / delete / pin one memory
            if b.get("delete"):
                S.delete_memory(int(b["id"]))
            elif b.get("id"):
                S.update_memory(int(b["id"]), b.get("text"), b.get("category"), b.get("importance"), b.get("pinned"))
            else:
                S.add_memory(b.get("text", ""), b.get("category", "other"), int(b.get("importance") or 4), source="user")
        elif action == "style":
            S.set("style", {k: v for k, v in (b.get("style") or {}).items() if v})
        elif action == "profile":
            p = companion.profile()
            p.update({k: b[k] for k in ("name", "persona", "custom", "memory_on") if k in b})
            S.set("profile", p)
        elif action == "reflect":
            return self.send(200, {"ok": companion.reflect(force=True)})
        elif action == "forget":
            S.forget_all()
        else:
            return self.send(404, {"error": "introuvable"})
        self.send(200, {"ok": True})


_json = lambda f: lambda h, p: h.send(200, f())  # noqa: E731  routes that only return data
GET = {"/": Handler.page, "/index.html": Handler.page, "/logo.png": Handler.logo, "/api/health": Handler.health,
       "/api/metrics": _json(metrics), "/api/sessions": _json(harness.Session.list_all), "/api/state": Handler.state,
       "/api/projzip": Handler.projzip, "/api/settings": Handler.settings, "/api/syscheck": _json(syscheck.current),
       "/api/bot": Handler.bot_state, "/api/voice": Handler.voice, "/api/localscan": _json(localscan.status),
       "/api/attach": Handler.attach_status, "/api/cloud": _json(cloud.status), "/api/cloud/models": Handler.cloud_models,
       "/api/version": _json(updater.status), "/api/switch": Handler.switch_state,
       "/api/hf/search": Handler.hf, "/api/hf/files": Handler.hf, "/api/search": Handler.search_chats,
       "/api/export": Handler.export, "/api/workspace": Handler.workspace_get, "/api/tts": Handler.tts_status,
       "/api/studio": Handler.studio_lib}
GET_PREFIX = [("/api/img/", Handler.image), ("/vendor/", Handler.vendor), ("/api/studio/file/", Handler.studio_file)]
POST = {"/api/new": Handler.new_session, "/api/delete": Handler.delete_session, "/api/install": Handler.install,
        "/api/syscheck": Handler.rescan, "/api/projrun": Handler.projrun, "/api/onboarded": Handler.onboarded,
        "/api/use": Handler.use, "/api/restart": Handler.restart,"/api/attach": Handler.attach_add, "/api/cloud/key": Handler.cloud_key,
        "/api/cloud/remove": Handler.cloud_remove, "/api/cloud/add": Handler.cloud_add,
        "/api/localscan": Handler.localscan_start, "/api/localscan/use": Handler.localscan_use,
        "/api/version/check": Handler.version_check, "/api/update": Handler.update, "/api/hf/install": Handler.hf_install,
        "/api/model/delete": Handler.model_delete, "/api/settings": Handler.save_settings, "/api/chat": Handler.chat,
        "/api/import": Handler.import_chat, "/api/meta": Handler.chat_meta, "/api/config": Handler.chat_config,
        "/api/version/switch": Handler.version, "/api/redo": Handler.redo, "/api/workspace": Handler.workspace_save,
        "/api/tts": Handler.tts_speak, "/api/tts/install": Handler.tts_install, "/api/approve": Handler.approve}
POST_PREFIX = [("/api/transcribe", Handler.transcribe), ("/api/bot/", Handler.bot), ("/api/studio/", Handler.studio_post)]


class PreviewHandler(Handler):
    """Generated projects, on their own origin (port + 1): their code cannot call the API above."""

    def project(self, path):  # /api/proj/<session>/<project>/<path>
        import mimetypes
        try:
            sid, pid, fpath = unquote(path[len("/api/proj/"):]).split("/", 2)
            s = get_session(sid)
            files = s.projects[pid]["files"]
            img = s.asset_file(pid, fpath) if fpath else None
            if img and os.path.exists(img):  # generated image used by the project
                with open(img, "rb") as f:
                    return self.send(200, f.read(), mimetypes.guess_type(fpath)[0] or "image/png")
            body = files.get(fpath) if fpath else None
            if body is None:
                return self.send(404, b"not found", "text/plain")
            ctype = mimetypes.guess_type(fpath)[0] or "text/plain"
            text = ctype.startswith(("text/", "application/javascript", "application/json", "image/svg"))
            self.send(200, body.encode(), ctype + ("; charset=utf-8" if text else ""))
        except (AttributeError, KeyError, ValueError):
            self.send(404, b"not found", "text/plain")

    def do_GET(self):
        self.dispatch({}, [("/api/proj/", PreviewHandler.project)])

    def do_POST(self):
        self.send(403, {"error": "requête refusée"})


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


def first_assistant():
    """Loads an assistant at startup (through the switch, so the UI shows the loading steps)."""
    if alive(harness.LLM + "/health"):  # llama-server still running from a previous session
        llm_state["name"] = harness.SETTINGS["llm"]
        return
    ok, rec = installed_llms(), syscheck.current().get("recommended", {}).get("llm")
    pick = next((i for i in (harness.SETTINGS["llm"], rec) if i in ok), ok[0] if ok else None)
    if pick:
        use_model(pick)
    else:
        print("Aucun assistant installe : choisis-en un dans l'interface.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=config.APP_PORT)
    ap.add_argument("--no-llm", action="store_true")
    ap.add_argument("--no-comfy", action="store_true")
    a = ap.parse_args()
    config.APP_PORT, config.PREVIEW_PORT = a.port, a.port + 1
    harness.load_settings()
    if harness.SETTINGS.get("llm") and harness.SETTINGS["llm"] not in LLMS:
        harness.SETTINGS["llm"] = None  # old setting pointing to a model removed from the catalog
    shutil.rmtree(os.path.join(harness.DATA, "_tmp"), ignore_errors=True)  # leftovers from ephemeral conversations
    syscheck.current()
    import companion  # noqa: F401  Sparky Bot: opens its memory and starts the background reflection loop
    if not a.no_comfy:
        start_comfy()
    if not a.no_llm:
        try:
            first_assistant()
        except Exception as e:
            print(e)
        threading.Thread(target=autostart, daemon=True).start()
    updater.start()
    attach.server_voice = voice_model
    openai_api.active_model = lambda: (llm_state["name"], LLMS[llm_state["name"]]["label"]) if llm_state["name"] in LLMS else None
    threading.Thread(target=mcp.start_all, daemon=True).start()
    scheduler.start(run_scheduled)
    studio.hooks.update(before_video=pause_llm_for_video, after_queue=resume_llm_after_video)
    preview = Server(("127.0.0.1", config.PREVIEW_PORT), PreviewHandler)
    threading.Thread(target=preview.serve_forever, daemon=True).start()
    print(f"Sparky {updater.current()} : http://127.0.0.1:{a.port}")
    Server(("127.0.0.1", a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
