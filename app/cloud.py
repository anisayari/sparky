"""Optional cloud models (OpenRouter, Anthropic, OpenAI, Gemini, Mistral, Groq, DeepSeek, xAI, any
OpenAI-compatible server). Off by default: Sparky stays 100% local until the user adds a key.

Keys are stored on this PC only (data/cloud.json). Claude goes through the official `anthropic` SDK;
the other providers speak the OpenAI chat-completions format.

Everything Sparky sends to the assistant (messages, images, attached files) then goes to the provider.
"""
import contextlib, json, os, re, threading, urllib.error, urllib.request

import config
import turn_runtime

FILE = os.path.join(config.DATA, "cloud.json")
PROVIDERS = {   # id -> label, OpenAI-compatible base URL, where to get a key
    "openrouter": {"label": "OpenRouter", "base": "https://openrouter.ai/api/v1", "keys": "https://openrouter.ai/keys"},
    "anthropic": {"label": "Anthropic (Claude)", "base": None, "keys": "https://console.anthropic.com/settings/keys"},
    "openai": {"label": "OpenAI", "base": "https://api.openai.com/v1", "keys": "https://platform.openai.com/api-keys"},
    "gemini": {"label": "Google Gemini", "base": "https://generativelanguage.googleapis.com/v1beta/openai",
               "keys": "https://aistudio.google.com/apikey"},
    "mistral": {"label": "Mistral", "base": "https://api.mistral.ai/v1", "keys": "https://console.mistral.ai/api-keys"},
    "groq": {"label": "Groq", "base": "https://api.groq.com/openai/v1", "keys": "https://console.groq.com/keys"},
    "deepseek": {"label": "DeepSeek", "base": "https://api.deepseek.com/v1", "keys": "https://platform.deepseek.com/api_keys"},
    "xai": {"label": "xAI (Grok)", "base": "https://api.x.ai/v1", "keys": "https://console.x.ai"},
    "custom": {"label": "Compatible OpenAI (perso)", "base": "", "keys": ""},  # LM Studio, vLLM, a company proxy...
}
JSON_OK = {"openrouter", "openai", "mistral", "deepseek", "gemini", "xai"}  # accept response_format json_object
# Claude models that accept the server-side refusal fallback ("default" mode, Claude API)
FALLBACK_MODELS = ("claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5")
EFFORT = {"off": "low", "low": "low", "medium": "medium", "high": "high"}

_lock = threading.Lock()


# ---------------------------------------------------------------- keys
_cache = {"mtime": None, "data": {}}


def _load():
    """Keys file, re-read only when it changed (it is read for every request and every catalog entry)."""
    try:
        mtime = os.path.getmtime(FILE)
    except OSError:
        return {}
    if mtime != _cache["mtime"]:
        _cache.update(mtime=mtime, data=config.read_json(FILE, {}) or {})
    return {k: dict(v) for k, v in _cache["data"].items()}


def _save(d):
    config.write_json(FILE, d)
    _cache["mtime"] = None


def conf(pid):
    return _load().get(pid, {})


def has_key(pid):
    c = conf(pid)
    return bool(c.get("key") or (pid == "custom" and c.get("base")))


def base_url(pid):
    return (conf(pid).get("base") if pid == "custom" else PROVIDERS[pid]["base"]) or ""


def mask(k):
    return (k[:6] + "…" + k[-4:]) if k and len(k) > 12 else ("••••" if k else "")


def status():
    d = _load()
    return [{"id": p, "label": v["label"], "keys": v["keys"], "hasKey": has_key(p), "masked": mask(d.get(p, {}).get("key", "")),
             "base": d.get(p, {}).get("base", "") if p == "custom" else v["base"]} for p, v in PROVIDERS.items()]


def set_key(pid, key, base=None):
    if pid not in PROVIDERS:
        raise RuntimeError("fournisseur inconnu")
    with _lock:
        d = _load()
        c = d.setdefault(pid, {})
        if pid == "custom" and base is not None:
            base = base.strip().rstrip("/")
            if base != c.get("base") and key is None:
                c.pop("key", None)  # never send the saved key to a new address
            c["base"] = base
        if key is not None:
            c["key"] = key.strip()
        _save(d)
    models(pid)  # checks the key right away (raises a clear error if it is refused)


def remove(pid):
    with _lock:
        d = _load()
        d.pop(pid, None)
        _save(d)


# ---------------------------------------------------------------- errors
def _friendly(pid, code, text):
    p = PROVIDERS[pid]["label"]
    if code in (401, 403):
        return f"clé API refusée par {p} : vérifie-la dans Paramètres > Modèles cloud"
    if code == 402:
        return f"crédit insuffisant chez {p}"
    if code == 404:
        return f"modèle introuvable chez {p}"
    if code == 429:
        return f"limite de requêtes atteinte chez {p}, réessaie dans un instant"
    m = re.search(r'"message"\s*:\s*"([^"]+)', text or "")
    return f"{p} : erreur {code} — " + (m.group(1) if m else (text or "")[:300])


# ---------------------------------------------------------------- OpenAI-compatible providers
def _headers(pid):
    h = {"Content-Type": "application/json"}
    if conf(pid).get("key"):
        h["Authorization"] = "Bearer " + conf(pid)["key"]
    if pid == "openrouter":
        h |= {"HTTP-Referer": "https://github.com/anisayari/sparky", "X-Title": "Sparky"}
    return h


def _get(pid, path):
    req = urllib.request.Request(base_url(pid) + path, headers=_headers(pid))
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise RuntimeError(_friendly(pid, e.code, e.read().decode(errors="replace"))) from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"connexion impossible à {PROVIDERS[pid]['label']} ({e.reason})") from None


VISION_HINT = re.compile(r"gpt-4o|gpt-4\.1|gpt-5|o3|o4|gemini|claude|pixtral|vision|-vl|llama-4|grok-4|grok-2-vision|qwen.*vl", re.I)
REASONING_HINT = re.compile(r"^(o\d|gpt-5)|reason|r1|thinking|grok-4|magistral", re.I)


def _oai_stream(pid, model, messages, max_tokens, think, json_mode, admission):
    body = {"model": model, "messages": messages, "stream": True}
    body["max_completion_tokens" if pid == "openai" else "max_tokens"] = max_tokens
    extra = {}
    if json_mode and pid in JSON_OK:
        extra["response_format"] = {"type": "json_object"}
    if think != "off":
        if pid == "openrouter":
            extra["reasoning"] = {"effort": think}
        elif pid in ("openai", "xai", "groq") and REASONING_HINT.search(model):
            extra["reasoning_effort"] = think
    else:
        extra["temperature"] = 0.4
    for attempt in ((body | extra, body) if extra else (body,)):  # a 400 on an optional parameter: retry bare
        req = urllib.request.Request(base_url(pid) + "/chat/completions", json.dumps(attempt).encode(), _headers(pid))
        try:
            with admission():
                r = urllib.request.urlopen(req, timeout=900)
        except urllib.error.HTTPError as e:
            text = e.read().decode(errors="replace")
            if e.code in (400, 422) and attempt is not body:
                continue
            raise RuntimeError(_friendly(pid, e.code, text)) from None
        except urllib.error.URLError as e:
            raise RuntimeError(f"connexion impossible à {PROVIDERS[pid]['label']} ({e.reason})") from None
        with r, turn_runtime.response(r):
            for raw in r:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:") or line == "data: [DONE]":
                    continue
                try:
                    chunk = json.loads(line[5:].strip())
                except ValueError:
                    continue
                if chunk.get("error"):
                    raise RuntimeError(_friendly(pid, chunk["error"].get("code", 0), json.dumps(chunk["error"])))
                for ch in chunk.get("choices") or []:
                    d = ch.get("delta") or {}
                    yield d.get("content") or "", d.get("reasoning") or d.get("reasoning_content") or ""
        return


def _oai_models(pid):
    out = []
    for m in _get(pid, "/models").get("data", []):
        mid = m.get("id", "")
        mods = (m.get("architecture") or {}).get("input_modalities")
        params = m.get("supported_parameters")
        out.append({"id": mid, "label": m.get("name") or mid,
                    "vision": ("image" in mods) if mods is not None else bool(VISION_HINT.search(mid)),
                    "think": ("reasoning" in params) if params is not None else bool(REASONING_HINT.search(mid)),
                    "context": m.get("context_length")})
    return sorted(out, key=lambda x: x["id"])


# ---------------------------------------------------------------- Anthropic (official SDK)
def _client():
    import anthropic
    return anthropic.Anthropic(api_key=conf("anthropic").get("key"), max_retries=2)


def _caps(m):
    c = getattr(m, "capabilities", None) or {}
    if not isinstance(c, dict):
        c = c.to_dict() if hasattr(c, "to_dict") else {}
    leaf = lambda *ks: _dig(c, ks)
    return {"vision": leaf("image_input", "supported") is not False,
            "think": bool(leaf("thinking", "types", "adaptive", "supported")),
            "effort": bool(leaf("effort", "supported"))}


def _dig(d, ks):
    for k in ks:
        if not isinstance(d, dict) or k not in d:
            return None
        d = d[k]
    return d


def _claude_models():
    import anthropic
    try:
        return [{"id": m.id, "label": m.display_name, "context": getattr(m, "max_input_tokens", None), **_caps(m)}
                for m in _client().models.list()]
    except anthropic.APIStatusError as e:
        raise RuntimeError(_friendly("anthropic", e.status_code, str(e.message))) from None
    except anthropic.APIConnectionError:
        raise RuntimeError("connexion impossible à Anthropic (Internet ?)") from None


def _to_claude(messages):
    """OpenAI-style messages (Sparky's internal format) -> system + Anthropic content blocks."""
    system, out = [], []
    for m in messages:
        c = m["content"]
        if m["role"] == "system":
            system.append(c if isinstance(c, str) else " ".join(p.get("text", "") for p in c))
            continue
        if isinstance(c, list):
            blocks = []
            for p in c:
                if p.get("type") == "text" and p.get("text"):
                    blocks.append({"type": "text", "text": p["text"]})
                elif p.get("type") == "image_url":
                    url = p["image_url"]["url"]
                    mt = re.match(r"data:([^;]+);base64,(.*)", url, re.S)
                    blocks.append({"type": "image", "source": {"type": "base64", "media_type": mt.group(1), "data": mt.group(2)}}
                                  if mt else {"type": "image", "source": {"type": "url", "url": url}})
            c = blocks or [{"type": "text", "text": "…"}]
        out.append({"role": m["role"], "content": c})
    if out and out[0]["role"] != "user":
        out.insert(0, {"role": "user", "content": "(début de la conversation)"})
    return "\n\n".join(system), out


def _claude_stream(meta, messages, max_tokens, think, admission):
    import anthropic
    system, msgs = _to_claude(messages)
    model = meta["model"]
    kw = {"model": model, "max_tokens": max_tokens, "messages": msgs}
    if system:
        kw["system"] = system
    opt = {}
    if meta.get("think") and think != "off":
        opt["thinking"] = {"type": "adaptive", "display": "summarized"}  # summarized: the UI shows the reasoning
    if meta.get("effort"):
        opt["output_config"] = {"effort": EFFORT.get(think, "low")}
    if model in FALLBACK_MODELS:  # a declined request is re-run by Anthropic on a fallback model
        opt["extra_headers"] = {"anthropic-beta": "server-side-fallback-2026-07-01"}
        opt["extra_body"] = {"fallbacks": "default"}
    client = _client()
    if admission is not contextlib.nullcontext:
        client = client.with_options(max_retries=0)
    for attempt in ((kw | opt, kw) if opt else (kw,)):
        try:
            with contextlib.ExitStack() as stack:
                with admission():
                    s = stack.enter_context(client.messages.stream(**attempt))
                stack.enter_context(turn_runtime.response(s))
                for ev in s:
                    if ev.type == "content_block_delta":
                        if ev.delta.type == "text_delta":
                            yield ev.delta.text, ""
                        elif ev.delta.type == "thinking_delta":
                            yield "", ev.delta.thinking
                final = s.get_final_message()
            if final.stop_reason == "refusal":
                raise RuntimeError("Claude a refusé de répondre à cette demande")
            return
        except anthropic.BadRequestError as e:
            if attempt is not kw:  # an option this model does not take (effort, thinking...): retry without
                continue
            raise RuntimeError(_friendly("anthropic", 400, str(e.message))) from None
        except anthropic.APIStatusError as e:
            raise RuntimeError(_friendly("anthropic", e.status_code, str(e.message))) from None
        except anthropic.APIConnectionError:
            raise RuntimeError("connexion impossible à Anthropic (Internet ?)") from None


# ---------------------------------------------------------------- public API
def models(pid):
    if pid not in PROVIDERS:
        raise RuntimeError("fournisseur inconnu")
    if not has_key(pid):
        raise RuntimeError("ajoute d'abord une clé API")
    return _claude_models() if pid == "anthropic" else _oai_models(pid)


def stream(meta, messages, max_tokens=8000, think="off", json_mode=False, admission=contextlib.nullcontext):
    """Yields (text, reasoning) pieces. meta: the catalog entry of a cloud model."""
    if meta["provider"] == "anthropic":
        yield from _claude_stream(meta, messages, max_tokens, think, admission)
    else:
        yield from _oai_stream(meta["provider"], meta["model"], messages, max_tokens, think, json_mode, admission)


def complete(meta, messages, max_tokens=900, json_mode=False):
    return "".join(t for t, _ in stream(meta, messages, max_tokens, "off", json_mode)).strip()


def ping(meta):
    """Short request to check the key and the model before switching to it."""
    complete(meta, [{"role": "user", "content": "Reply with: OK"}], max_tokens=16)


def entry(pid, model, label=None, vision=False, think=False, effort=False):
    """Catalog entry for a cloud model (saved with the custom models)."""
    p = PROVIDERS[pid]["label"]
    return {"id": f"cloud:{pid}:{model}", "kind": "llm", "label": (label or model) + " ☁", "quality": 5, "repo": "",
            "desc": f"Modèle cloud · {p} · {model}", "files": [], "size_gb": 0, "active_gb": 0, "ref_tps": None,
            "think": bool(think), "vision": bool(vision), "effort": bool(effort), "custom": True, "cloud": True,
            "provider": pid, "model": model, "providerLabel": p}
