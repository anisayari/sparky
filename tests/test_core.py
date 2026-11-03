"""Catalog and agent helpers (pure functions, no model needed)."""
import pytest

import catalog
import harness


# ---------------------------------------------------------------- catalog
@pytest.mark.parametrize("name,total,active", [("Qwen3.5-35B-A3B", 35, 3), ("gemma-4-12b-it", 12, None),
                                               ("Qwen3.5-4B", 4, None), ("no-size", None, None)])
def test_params(name, total, active):
    assert catalog._params(name) == (total, active)


def test_quality_guess_grows_with_size():
    assert [catalog.quality_guess(x) for x in (None, 3, 9, 30, 120)] == [3, 2, 3, 4, 5]


@pytest.mark.parametrize("text", ["Some-Uncensored-7B", "model-ABLITERATED", "nsfw-xl"])
def test_blocked_models(text):
    assert catalog.blocked(text)


def test_official_models_are_not_blocked():
    assert not any(catalog.blocked(m["id"], m.get("repo", "")) for m in catalog.CATALOG)


def test_catalog_ids_are_unique_and_complete():
    ids = [m["id"] for m in catalog.CATALOG]
    assert len(ids) == len(set(ids))
    for m in catalog.CATALOG:
        assert m["kind"] in ("llm", "image", "voice", "embed", "video") and m["label"] and m["size_gb"] > 0


def test_nothing_is_installed_on_a_fresh_machine():
    assert not [m["id"] for m in catalog.CATALOG if not m.get("custom") and catalog.installed(m)]


def test_cloud_model_needs_a_key(clean_cloud):
    m = clean_cloud.entry("openrouter", "x/y", vision=True)
    assert not catalog.installed(m)
    clean_cloud._save({"openrouter": {"key": "k"}})
    assert catalog.installed(m) and catalog.has_vision(m)
    assert catalog.indicators(m)["fit"] == 2  # uses no memory on this PC


# ---------------------------------------------------------------- agent helpers
def closed(text):
    st = {"pos": 0, "depth": 0, "str": False, "esc": False, "done": False}
    return harness.json_closed(text, st), st["pos"]


def test_json_closed_ignores_braces_in_strings():
    assert closed('{"a": "}{"}') == (True, 11)
    assert closed('{"a": {"b": 1}') == (False, 14)
    assert closed('note {"a": "\\"}"} trailing')[0]


def test_json_closed_streaming_is_incremental():
    st = {"pos": 0, "depth": 0, "str": False, "esc": False, "done": False}
    assert not harness.json_closed('{"x": "a', st)
    assert harness.json_closed('{"x": "a"} rest', st) and st["pos"] == 10


def test_split_actions_keeps_packed_actions_in_order():
    """Regression: an artifact followed by a reply in ONE object lost the artifact."""
    acts = harness.split_actions('{"action": "artifact", "args": {"path": "a.html"}, "action": "reply", "args": {"text": "ok"}}')
    assert [a["action"] for a in acts] == ["artifact", "reply"] and acts[0]["args"]["path"] == "a.html"


@pytest.mark.parametrize("raw,clean", [("../../etc/passwd", "etc/passwd"), ("C:\\Windows\\x.dll", "Windows/x.dll"),
                                       ("css/style.css", "css/style.css"), ("a b/c?.js", "a_b/c_.js")])
def test_clean_path_stays_inside_the_project(raw, clean):
    assert harness.clean_path(raw) == clean


def test_check_content_refuses_placeholders():
    with pytest.raises(RuntimeError):
        harness.check_content("app.js", "// TODO")
    harness.check_content("app.js", "function main() { console.log('a real program here'); }\nmain();")


@pytest.mark.parametrize("text,lang", [("Bonjour, peux-tu m'aider à écrire une lettre ?", "French"),
                                       ("Hello, could you help me write a letter please?", "English")])
def test_detect_lang(text, lang):
    assert harness.detect_lang(text) == lang


def test_chat_routes_to_the_active_cloud_model(clean_cloud, mock_provider, monkeypatch):
    clean_cloud.set_key("custom", "test-key", mock_provider.url + "/oai")
    m = clean_cloud.entry("custom", "mock/vision-chat")
    monkeypatch.setitem(catalog.BY_ID, m["id"], m)
    monkeypatch.setitem(harness.SETTINGS, "llm", m["id"])
    assert harness.cloud_model() is m
    assert harness.chat_once({"messages": [{"role": "user", "content": "x"}], "max_tokens": 10}) == "Bonjour"
    assert "".join(t for t, _ in harness.chat_deltas({"messages": [{"role": "user", "content": "x"}]})) == "Bonjour"


def test_plain_chat_keeps_the_latest_input_and_uses_available_tools(monkeypatch, tmp_path):
    import config
    monkeypatch.setattr(config, "COMFY_DIR", str(tmp_path / "missing-studio"))
    monkeypatch.setattr(config, "EXTERNAL_COMFY", False)
    session = harness.Session(ephemeral=True)
    session.lang = "French"
    session.history = [{"role": "user", "text": "Give the capital of France.", "raw": "Give the capital of France."}]
    messages = session.llm_messages()
    assert '"action":"reply"' in messages[0]["content"]
    assert "Current image model:" not in messages[0]["content"]
    assert messages[-1]["content"] == [{"type": "text", "text": "Give the capital of France."}]
