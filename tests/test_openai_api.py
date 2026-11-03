"""OpenAI-compatible API: auth, models, chat (local proxy to a fake llama-server, and cloud), streaming format."""
import json, os, threading, urllib.error, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


class FakeLlama(BaseHTTPRequestHandler):
    """llama-server stand-in: answers "Bonjour" (streamed in two pieces or whole)."""
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
        self.server.requests.append(body)
        if not body.get("stream"):
            out = json.dumps({"id": "x", "object": "chat.completion", "created": 1, "model": "gguf",
                              "choices": [{"index": 0, "message": {"role": "assistant", "content": "Bonjour"},
                                           "finish_reason": "stop"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            return self.wfile.write(out)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for d, fin in (({"role": "assistant", "content": "Bon"}, None), ({"content": "jour"}, None), ({}, "stop")):
            chunk = {"id": "x", "object": "chat.completion.chunk", "created": 1, "model": "gguf",
                     "choices": [{"index": 0, "delta": d, "finish_reason": fin}]}
            self.wfile.write(b"data: " + json.dumps(chunk).encode() + b"\n\n")
        self.wfile.write(b"data: [DONE]\n\n")


class Front(BaseHTTPRequestHandler):
    """Minimal stand-in for Sparky's Handler: routes /v1/ to openai_api.handle."""
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        import openai_api
        openai_api.handle(self, self.path)

    do_POST = do_GET


def _serve(handler):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    srv.requests = []
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.fixture
def api(data_dir):
    import openai_api
    if os.path.exists(openai_api.FILE):
        os.remove(openai_api.FILE)
    llama, front = _serve(FakeLlama), _serve(Front)
    old = openai_api.LLM, openai_api.active_model
    openai_api.LLM = f"http://127.0.0.1:{llama.server_address[1]}"
    openai_api.active_model = lambda: ("qwen3-8b", "Qwen3 8B")
    key = openai_api.set_enabled(True)["key"]
    yield f"http://127.0.0.1:{front.server_address[1]}", key, llama
    openai_api.LLM, openai_api.active_model = old
    llama.shutdown(), front.shutdown()
    os.remove(openai_api.FILE)


def call(url, body=None, key=None, raw=False):
    headers = {"Content-Type": "application/json"} | ({"Authorization": "Bearer " + key} if key else {})
    req = urllib.request.Request(url, json.dumps(body).encode() if body is not None else None, headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            data = r.read()
            return r.status, (data if raw else json.loads(data))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def sse(raw):
    lines = [x[6:] for x in raw.decode().split("\n\n") if x.startswith("data: ")]
    assert lines[-1] == "[DONE]"
    return [json.loads(x) for x in lines[:-1]]


def test_key_is_created_and_regenerated(data_dir):
    import openai_api
    k1 = openai_api.settings()["key"]
    assert k1.startswith("sk-sparky-") and openai_api.settings()["key"] == k1
    k2 = openai_api.regenerate_key()["key"]
    assert k2 != k1 and openai_api.settings()["key"] == k2


def test_auth(api):
    base, key, _ = api
    code, e = call(base + "/v1/models")
    assert code == 401 and e["error"]["type"] == "authentication_error"
    assert call(base + "/v1/models", key="sk-sparky-wrong")[0] == 401
    import openai_api
    openai_api.set_enabled(False)
    code, e = call(base + "/v1/models", key=key)
    assert code == 403 and "message" in e["error"]


def test_models(api):
    base, key, _ = api
    code, m = call(base + "/v1/models", key=key)
    assert code == 200 and m["object"] == "list" and [x["id"] for x in m["data"]] == ["qwen3-8b"]


def test_no_model_loaded(api):
    base, key, _ = api
    import openai_api
    openai_api.active_model = lambda: None
    assert call(base + "/v1/models", key=key)[1]["data"] == []
    assert call(base + "/v1/chat/completions", {"messages": [{"role": "user", "content": "hi"}]}, key)[0] == 503


def test_unknown_endpoint_and_bad_request(api):
    base, key, _ = api
    assert call(base + "/v1/nope", key=key)[0] == 404
    assert call(base + "/v1/chat/completions", {"model": "x"}, key)[0] == 400


def test_local_non_stream(api):
    base, key, llama = api
    code, r = call(base + "/v1/chat/completions", {"model": "whatever", "messages": [{"role": "user", "content": "hi"}],
                                                   "temperature": 0.2, "tools": [{"type": "function"}]}, key)
    assert code == 200 and r["model"] == "qwen3-8b" and r["choices"][0]["message"]["content"] == "Bonjour"
    sent = llama.requests[-1]
    assert sent["temperature"] == 0.2 and "tools" not in sent and sent["stream"] is False


def test_local_stream(api):
    base, key, _ = api
    code, raw = call(base + "/v1/chat/completions", {"messages": [{"role": "user", "content": "hi"}], "stream": True}, key, raw=True)
    chunks = sse(raw)
    assert code == 200 and all(c["model"] == "qwen3-8b" and c["object"] == "chat.completion.chunk" for c in chunks)
    assert "".join(c["choices"][0]["delta"].get("content", "") for c in chunks) == "Bonjour"
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"


def test_cloud_stream_and_once(api, monkeypatch):
    base, key, llama = api
    import harness
    monkeypatch.setattr(harness, "cloud_model", lambda: {"cloud": True})
    monkeypatch.setattr(harness, "chat_deltas", lambda body, think="off": iter([("", "hmm"), ("Sa", ""), ("lut", "")]))
    n = len(llama.requests)
    code, raw = call(base + "/v1/chat/completions", {"messages": [{"role": "user", "content": "hi"}], "stream": True}, key, raw=True)
    chunks = sse(raw)
    assert code == 200 and chunks[0]["choices"][0]["delta"]["role"] == "assistant"
    assert "".join(c["choices"][0]["delta"].get("content", "") for c in chunks) == "Salut"
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop" and len({c["id"] for c in chunks}) == 1
    code, r = call(base + "/v1/chat/completions", {"messages": [{"role": "user", "content": "hi"}]}, key)
    assert code == 200 and r["object"] == "chat.completion" and r["choices"][0]["message"]["content"] == "Salut"
    assert len(llama.requests) == n  # llama-server was not used


def test_cloud_error_in_stream(api, monkeypatch):
    base, key, _ = api
    import harness

    def boom(body, think="off"):
        yield "a", ""
        raise RuntimeError("quota")
    monkeypatch.setattr(harness, "cloud_model", lambda: {"cloud": True})
    monkeypatch.setattr(harness, "chat_deltas", boom)
    _, raw = call(base + "/v1/chat/completions", {"messages": [{"role": "user", "content": "hi"}], "stream": True}, key, raw=True)
    assert "quota" in sse(raw)[-1]["error"]["message"]


@pytest.mark.parametrize("remote", [False, True])
def test_busy_counter_released(api, monkeypatch, remote):
    base, key, _ = api
    import harness, openai_api
    observed = []
    publish = openai_api._json
    def counted_response(*args):
        observed.append(harness.ACTIVITY["llm"])
        return publish(*args)
    monkeypatch.setattr(openai_api, "_json", counted_response)
    if remote:
        monkeypatch.setattr(harness, "cloud_model", lambda: {"cloud": True})
        monkeypatch.setattr(harness, "chat_deltas", lambda body: iter([("test", "")]))
    call(base + "/v1/chat/completions", {"messages": [{"role": "user", "content": "hi"}]}, key)
    assert observed == [0] and harness.ACTIVITY["llm"] == 0
