r"""Sparky paths.

By default everything lives in the project folder (runtime\, models\, data\). A config.json file
at the project root can point to an existing installation, for example:
  {"comfy_dir": "D:\\ComfyUI", "llama_server": "D:\\llama\\llama-server.exe", "llm_dir": "D:\\models"}
"""
import json, os, threading

APP = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(APP)
_cfg = {}
CONFIG_FILE = os.environ.get("SPARKY_CONFIG") or os.path.join(ROOT, "config.json")  # SPARKY_CONFIG: alternate config
if os.path.exists(CONFIG_FILE):
    _cfg = json.load(open(CONFIG_FILE, encoding="utf-8-sig"))


def _p(key, default):
    return os.path.abspath(os.path.expandvars(_cfg.get(key) or default))


RUNTIME = _p("runtime_dir", os.path.join(ROOT, "runtime"))
COMFY_DIR = _p("comfy_dir", os.path.join(RUNTIME, "ComfyUI"))
LLAMA_SERVER = _p("llama_server", os.path.join(RUNTIME, "llama", "llama-server.exe"))
LLM_DIR = _p("llm_dir", os.path.join(ROOT, "models", "llm"))
MMPROJ_DIR = _p("mmproj_dir", os.path.join(ROOT, "models", "mmproj"))
VOICE_DIR = _p("voice_dir", os.path.join(ROOT, "models", "voice"))
EMBED_DIR = _p("embed_dir", os.path.join(ROOT, "models", "embed"))
DATA = _p("data_dir", os.path.join(ROOT, "data"))
COMFY_MODELS = os.path.join(COMFY_DIR, "models")
COMFY_IN = os.path.join(COMFY_DIR, "input")
COMFY_OUT = os.path.join(COMFY_DIR, "output")
COMFY_PORT = int(_cfg.get("comfy_port", 8188))
LLM_PORT = int(_cfg.get("llm_port", 8080))
EXTERNAL_LLM = _cfg.get("allow_external_llm") is True
EXTERNAL_COMFY = _cfg.get("allow_external_comfy") is True
APP_PORT = int(_cfg.get("app_port", 7860))
PREVIEW_PORT = APP_PORT + 1  # generated projects are served on their own origin (they must not reach the API)
NOWIN = 0x08000000 if os.name == "nt" else 0  # subprocess.CREATE_NO_WINDOW

_io_lock = threading.Lock()


def read_json(path, default=None):
    """JSON file contents, or `default` when it is missing or damaged (Sparky must still start)."""
    try:
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path, obj):
    """Atomic write: the file is either the old or the new version, never half written (threads write it)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with _io_lock:
        data = json.dumps(obj, indent=1, ensure_ascii=False)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(data)
        os.replace(tmp, path)
