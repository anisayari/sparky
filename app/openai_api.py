"""OpenAI-compatible local API (/v1/models, /v1/chat/completions, /v1/embeddings) so other apps
(Continue, VS Code extensions, scripts) can use Sparky's active assistant.

Off by default. When enabled, every request must carry `Authorization: Bearer <key>` (data/api.json).
A local model is proxied to llama-server; a cloud model goes through harness.chat_deltas.
"""
import hmac, json, os, secrets, threading, time, urllib.error, urllib.request, uuid

import config
import harness

FILE = os.path.join(config.DATA, "api.json")
LLM = f"http://127.0.0.1:{config.LLM_PORT}"
MAX_BODY = 50 * 2**20
# OpenAI parameters forwarded to llama-server (tools, functions, logprobs... are dropped)
PASS = ("messages", "temperature", "top_p", "max_tokens", "max_completion_tokens", "stop", "seed", "presence_penalty",
        "frequency_penalty", "response_format", "n")

active_model = lambda: None  # noqa: E731  set by the integrator: () -> (id, label) or None
_lock = threading.Lock()


# ---------------------------------------------------------------- settings
def settings():
    s = config.read_json(FILE, {}) or {}
    if not s.get("key"):
        s = {"enabled": bool(s.get("enabled")), "key": _new_key()}
        config.write_json(FILE, s)
    return {"enabled": bool(s.get("enabled")), "key": s["key"]}


def _new_key():
    return "sk-sparky-" + secrets.token_urlsafe(24)


def set_enabled(on):
    with _lock:
        s = settings() | {"enabled": bool(on)}
        config.write_json(FILE, s)
    return s


def regenerate_key():
    with _lock:
        s = settings() | {"key": _new_key()}
        config.write_json(FILE, s)
    return s


def authorized(handler):
    """None when the request may go on, else (status, message)."""
    s = settings()
    if not s["enabled"]:
        return 403, "Sparky's OpenAI API is disabled (Settings > API)"
    auth = handler.headers.get("Authorization") or ""
    if not auth.startswith("Bearer ") or not hmac.compare_digest(auth[7:].strip().encode(), s["key"].encode()):
        return 401, "invalid API key"
    return None


# ---------------------------------------------------------------- HTTP plumbing
def _json(handler, code, obj):
    body = json.dumps(obj, ensure_ascii=False).encode()
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def _error(handler, code, msg, kind="invalid_request_error"):
    _json(handler, code, {"error": {"message": msg, "type": kind, "param": None, "code": None}})


def _body(handler):
    n = int(handler.headers.get("Content-Length") or 0)
    if n > MAX_BODY:
        raise OverflowError
    return json.loads(handler.rfile.read(n) or b"{}")


def _sse_open(handler):
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Connection", "close")
    handler.end_headers()
    handler.close_connection = True


def _sse(handler, obj):
    data = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)
    handler.wfile.write(f"data: {data}\n\n".encode())
    handler.wfile.flush()


# ---------------------------------------------------------------- routes
def handle(handler, path):
    """Entry point for every /v1/... request (GET or POST)."""
    denied = authorized(handler)
    if denied:
        return _error(handler, denied[0], denied[1], "permission_error" if denied[0] == 403 else "authentication_error")
    path = path.split("?")[0].rstrip("/")
    route = {("GET", "/v1/models"): models, ("POST", "/v1/chat/completions"): chat,
             ("POST", "/v1/embeddings"): embeddings}.get((handler.command, path))
    if not route:
        return _error(handler, 404, f"unknown endpoint {handler.command} {path}")
    try:
        route(handler)
    except OverflowError:
        _error(handler, 413, "request too large")
    except ValueError as e:
        _error(handler, 400, f"invalid JSON: {e}")


def models(handler):
    m = active_model()
    data = [{"id": m[0], "object": "model", "created": 0, "owned_by": "sparky", "name": m[1]}] if m else []
    _json(handler, 200, {"object": "list", "data": data})


def chat(handler):
    b = _body(handler)
    m = active_model()
    if not m:
        return _error(handler, 503, "no assistant is loaded in Sparky", "server_error")
    if not isinstance(b.get("messages"), list) or not b["messages"]:
        return _error(handler, 400, "'messages' is required")
    mid, stream = m[0], bool(b.get("stream"))
    with harness.busy("llm"):
        if harness.cloud_model():
            return _cloud(handler, b, mid, stream)
        return _local(handler, b, mid, stream)


def _local(handler, b, mid, stream):
    """Proxy to llama-server, model name rewritten to Sparky's id."""
    body = {k: b[k] for k in PASS if k in b} | {"model": mid, "stream": stream}
    # thinking: the client's reasoning_effort, else Sparky's own setting (a thinking model would otherwise spend
    # a small max_tokens on reasoning and return an empty answer)
    effort = b.get("reasoning_effort") or harness.THINK.get(harness.SETTINGS.get("think", "off"), (None, 0))[0]
    effort = None if effort in ("none", "off", "minimal") else effort
    body["chat_template_kwargs"] = b.get("chat_template_kwargs") or (
        {"enable_thinking": True, "reasoning_effort": effort} if effort else {"enable_thinking": False})
    if stream and b.get("stream_options"):
        body["stream_options"] = b["stream_options"]
    req = urllib.request.Request(LLM + "/v1/chat/completions", json.dumps(body).encode(), {"Content-Type": "application/json"})
    try:
        import process_owner
        process_owner.require_endpoint(req.full_url)
        r = urllib.request.urlopen(req, timeout=900)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return _json(handler, e.code, json.loads(raw))
        except ValueError:
            return _error(handler, e.code, raw.decode(errors="replace")[:600], "server_error")
    except OSError as e:
        return _error(handler, 503, f"local model unreachable: {e}", "server_error")
    with r:
        if not stream:
            out = json.load(r)
            out["model"] = mid
            return _json(handler, 200, out)
        _sse_open(handler)
        try:
            for raw in r:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                    chunk["model"] = mid
                except ValueError:
                    continue
                _sse(handler, chunk)
            _sse(handler, "[DONE]")
        except OSError:
            pass  # the client went away


def _cloud(handler, b, mid, stream):
    """Cloud assistant through harness.chat_deltas, re-encoded in the OpenAI format."""
    cid, created = "chatcmpl-" + uuid.uuid4().hex[:24], int(time.time())
    body = {"messages": b["messages"], "max_tokens": b.get("max_completion_tokens") or b.get("max_tokens") or 8000}
    if b.get("response_format", {}).get("type") == "json_object":
        body["response_format"] = b["response_format"]
    if not stream:
        try:
            text, reasoning = "", ""
            for t, rs in harness.chat_deltas(body):
                text, reasoning = text + t, reasoning + rs
        except Exception as e:
            return _error(handler, 502, f"cloud model error: {e}", "server_error")
        msg = {"role": "assistant", "content": text} | ({"reasoning_content": reasoning} if reasoning else {})
        return _json(handler, 200, {"id": cid, "object": "chat.completion", "created": created, "model": mid,
                                    "choices": [{"index": 0, "message": msg, "finish_reason": "stop"}],
                                    "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}})

    def chunk(delta, finish=None):
        return {"id": cid, "object": "chat.completion.chunk", "created": created, "model": mid,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
    _sse_open(handler)
    try:
        _sse(handler, chunk({"role": "assistant", "content": ""}))
        try:
            for t, rs in harness.chat_deltas(body):
                if rs:
                    _sse(handler, chunk({"reasoning_content": rs}))
                if t:
                    _sse(handler, chunk({"content": t}))
            _sse(handler, chunk({}, "stop"))
        except Exception as e:  # headers are sent: report the error in the stream
            _sse(handler, {"error": {"message": f"cloud model error: {e}", "type": "server_error"}})
        _sse(handler, "[DONE]")
    except OSError:
        pass  # the client went away


def embeddings(handler):
    b = _body(handler)
    import memory
    if not memory.EMB.ready():
        return _error(handler, 501, "no embedding model installed in Sparky (e5-small)", "server_error")
    inputs = b.get("input")
    inputs = [inputs] if isinstance(inputs, str) else inputs
    if not isinstance(inputs, list) or not all(isinstance(x, str) for x in inputs):
        return _error(handler, 400, "'input' must be a string or a list of strings")
    vecs = memory.EMB.encode(inputs) or []
    _json(handler, 200, {"object": "list", "model": memory.EMBED_ID,
                         "data": [{"object": "embedding", "index": i, "embedding": v} for i, v in enumerate(vecs)],
                         "usage": {"prompt_tokens": 0, "total_tokens": 0}})
