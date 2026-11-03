"""HTTP API smoke tests: a real Sparky server on a free port (no assistant, no ComfyUI)."""
import base64, json, threading, time, urllib.error, urllib.request

import pytest


@pytest.fixture(scope="module")
def base():
    import attach, server
    attach.server_voice = server.voice_model
    srv = server.Server(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def call(base, path, body=None, headers=None):
    req = urllib.request.Request(base + path, json.dumps(body).encode() if body is not None else None,
                                 {"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read()
            return r.status, json.loads(raw) if r.headers.get("Content-Type", "").startswith("application/json") else raw
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def test_page_is_served(base):
    code, html = call(base, "/")
    assert code == 200 and b"<title>" in html and b"Sparky" in html


def test_reviewed_summary_permits_a_fork_but_no_changes_to_its_transcript(base):
    import harness
    import server
    draft = harness.Session()
    draft.meta["workflow_run"] = "test-review"
    draft.turns = [(0, 0)]
    draft.history = [{"role": "user", "text": "input", "raw": "input"}, {"role": "assistant", "text": "summary"}]
    draft.events = [{"type": "user", "text": "input"}, {"type": "reply", "text": "summary"}, {"type": "done"}]
    draft.save()
    server.sessions[draft.id] = draft
    for route in ("/api/chat", "/api/redo", "/api/version/switch"):
        assert call(base, route, {"id": draft.id, "text": "replace", "k": 0, "j": 0})[0] == 409
    fork = draft.fork(0)
    assert not fork.meta.get("workflow_run") and fork.events[1]["text"] == "summary"


def test_settings_and_catalog(base):
    code, s = call(base, "/api/settings")
    assert code == 200 and {"catalog", "settings", "syscheck", "onboarded"} <= s.keys()
    assert all({"id", "kind", "installed"} <= m.keys() for m in s["catalog"])


def test_llm_context_setting_and_restart(base):
    import harness, server
    old = harness.SETTINGS.get("llm_ctx", 0)
    try:
        server.update_settings({"llm_ctx": "65536"})
        assert harness.SETTINGS["llm_ctx"] == 65536 and server.llm_ctx({"size_gb": 20}) == 65536
        server.update_settings({"llm_ctx": 10})  # clamped
        assert harness.SETTINGS["llm_ctx"] == server.CTX_MIN
        server.update_settings({"llm_ctx": 0})   # automatic
        assert harness.SETTINGS["llm_ctx"] == 0 and server.llm_ctx({"size_gb": 20}) == server.auto_ctx({"size_gb": 20})
        code, s = call(base, "/api/settings")
        assert code == 200 and {"ctx", "autoCtx"} <= s["active"].keys()
        assert call(base, "/api/restart", {"service": "nope"})[0] == 400
    finally:
        harness.SETTINGS["llm_ctx"] = old
        harness.save_settings()


def test_metrics_without_any_engine(base):
    code, m = call(base, "/api/metrics")
    assert code == 200 and m["llm"]["up"] is False and m["image"]["up"] is False and m["switch"]["phase"] == "idle"


def test_version(base):
    code, v = call(base, "/api/version")
    assert code == 200 and v["version"].count(".") == 2


def test_cloud_providers_listed_without_keys(base):
    code, ps = call(base, "/api/cloud")
    assert code == 200 and {p["id"] for p in ps} >= {"openrouter", "anthropic", "openai", "custom"}
    assert all("key" not in p for p in ps)


def test_cloud_add_needs_a_provider(base):
    assert call(base, "/api/cloud/add", {"provider": "nope", "model": "x"})[0] == 400


def test_attach_roundtrip(base):
    code, st = call(base, "/api/attach", {"name": "a.md", "type": "text/markdown", "data": base64.b64encode(b"# Titre").decode()})
    assert code == 200 and st["state"] in ("reading", "ok")
    for _ in range(50):
        st = call(base, "/api/attach?id=" + st["id"])[1]
        if st["state"] != "reading":
            break
        time.sleep(0.05)
    assert st["state"] == "ok" and st["chars"] == 7


def test_unknown_attachment(base):
    assert call(base, "/api/attach?id=nope")[0] == 404


def test_use_unknown_model_is_refused(base):
    assert call(base, "/api/use", {"id": "nope"}) == (200, {"ok": False})


def test_chat_without_assistant_explains_what_to_do(base):
    code, new = call(base, "/api/new", {"ephemeral": True})
    req = urllib.request.Request(base + "/api/chat", json.dumps({"id": new["id"], "text": "salut"}).encode(),
                                 {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        events = [json.loads(l[6:]) for l in r.read().decode().split("\n\n") if l.startswith("data: ")]
    assert any(e["type"] == "error" and "Aucun assistant" in e["text"] for e in events)
    assert events[-1]["type"] == "done"


def test_cross_site_requests_are_refused(base):
    """A web page must not be able to drive Sparky (install, update, keys...) from the user's browser."""
    code, _ = call(base, "/api/cloud/key", {"provider": "openrouter", "key": "x"}, {"Origin": "https://evil.example"})
    assert code == 403
    code, _ = call(base, "/api/version", headers={"Host": "evil.example:7860"})
    assert code == 403
