"""Companion memory consent, including opt-out while a provider request is in flight."""
import importlib.util
import json
import threading

import pytest

import memory


def test_forget_keeps_opt_out(bot):
    bot.update_profile({"memory_on": False})
    bot.forget_all()
    assert bot.profile()["memory_on"] is False
    assert not bot.reflect(force=True)


@pytest.mark.parametrize("change", ["disable", "forget"])
def test_foreground_context_revoked_before_provider_dispatch(bot, change):
    bot.S.set("summary", "previous private material")
    events = []

    def user_event(event):
        events.append(event["type"])
        if event["type"] == "context":
            if change == "disable":
                bot.update_profile({"memory_on": False})
            else:
                bot.forget_all()

    assert bot.chat("hello", user_event) == ""  # the fixture rejects any provider call
    assert events == ["context", "error"]


@pytest.fixture
def isolated_bot(tmp_path, monkeypatch):
    store = memory.Store(str(tmp_path / "companion.db"))
    # Load an independent companion backed only by this store. Keep the production
    # idle thread out of deterministic tests; exercise its reflect(force=False) entry directly.
    spec = importlib.util.spec_from_file_location("_test_companion", importlib.util.find_spec("companion").origin)
    companion = importlib.util.module_from_spec(spec)
    start = threading.Thread.start

    def start_unless_idle(thread):
        target = getattr(thread, "_target", None)
        if target is not None and target is getattr(companion, "idle_loop", None):
            return None
        return start(thread)

    monkeypatch.setattr(memory.EMB, "encode", lambda *args, **kwargs: None)
    with monkeypatch.context() as loader:
        loader.setattr(memory, "Store", lambda: store)
        loader.setattr(threading.Thread, "start", start_unless_idle)
        spec.loader.exec_module(companion)

    yield companion
    store.db.close()


@pytest.fixture
def bot(isolated_bot, monkeypatch):
    def unexpected_provider(*_args, **_kwargs):
        pytest.fail("Unexpected provider call")
    monkeypatch.setattr(isolated_bot, "llm", unexpected_provider)
    return isolated_bot


@pytest.mark.parametrize("provider", ["custom", "anthropic"])
@pytest.mark.parametrize("transition", ["unchanged", "disable", "forget", "delete_fact", "edit_style", "local_only"])
def test_consent_at_transport_open(isolated_bot, clean_cloud, mock_provider, monkeypatch, provider, transition):
    import mocks
    bot = isolated_bot
    if provider == "anthropic":
        monkeypatch.setenv("ANTHROPIC_BASE_URL", mock_provider.url)
    clean_cloud.set_key(provider, mocks.KEY, mock_provider.url + "/oai" if provider == "custom" else None)
    model = clean_cloud.entry(provider, "mock/model")
    monkeypatch.setattr(bot.harness, "cloud_model", lambda: model)
    original = clean_cloud.stream
    fact = bot.S.add_memory("synthetic memory", source="user")

    def delayed(*args, **kwargs):
        if transition == "disable":
            bot.update_profile({"memory_on": False})
        elif transition == "forget":
            bot.forget_all()
        elif transition == "delete_fact":
            bot.edit_memory({"id": fact, "delete": True})
        elif transition == "edit_style":
            bot.edit_style({})
        elif transition == "local_only":
            bot.update_profile({"local_only": True})
        yield from original(*args, **kwargs)

    monkeypatch.setattr(clean_cloud, "stream", delayed)
    reply = bot.llm([{"role": "user", "content": "synthetic memory"}])
    posts = [r for r in mock_provider.requests if r[0] == "POST"]
    assert bool(reply) == (transition == "unchanged")
    assert len(posts) == int(transition == "unchanged")
    if transition == "local_only":
        bot.forget_all()
        assert bot.profile()["local_only"]
        with pytest.raises(ValueError, match="local uniquement"):
            bot.chat("private input", lambda e: None)
        assert not bot.S.messages(1)


def test_opt_out_blocks_learning_background_reflection_and_personal_prompt(bot):
    store = bot.S
    store.add_memory("stored fact sentinel")
    store.set("style", {"tone": "stored style sentinel"})
    store.set("summary", "stored summary sentinel")
    store.set("checkin", "stored checkin sentinel")
    bot.update_profile({"memory_on": False})
    for i in range(5):
        store.add_message("user", f"opted-out message {i}")

    calls = []
    bot.llm = lambda *args, **kwargs: calls.append(args) or {}
    bot.learn("opted-out message", "an answer")
    assert not bot.reflect(force=False) and not bot.reflect(force=True)
    prompt, used = bot.system_prompt("stored fact")
    assert not calls and not used and "sentinel" not in prompt
    assert not store.journal()


def test_reenabled_reflection_uses_only_new_exchanges_and_learning_resumes(bot):
    store = bot.S
    store.add_message("user", "before opt out")
    bot.update_profile({"memory_on": False})
    store.add_message("user", "during opt out")
    bot.update_profile({"memory_on": True})
    store.add_message("user", "newly consented exchange")
    requests = []

    def reflect_model(messages, **_kwargs):
        requests.append(json.dumps(messages))
        return {"summary": "new summary", "journal": "new journal"}

    bot.llm = reflect_model
    assert bot.reflect(force=True)
    assert len(requests) == 1 and "newly consented exchange" in requests[0]
    assert "before opt out" not in requests[0] and "during opt out" not in requests[0]
    assert store.get("summary") == "new summary" and store.journal()[0]["text"] == "new journal"
    bot.llm = lambda *_args, **_kwargs: {"add": [{"text": "Collects amber fossils", "category": "preference"}]}
    bot.learn("I collect amber fossils.", "Understood.")
    assert any(m["text"] == "Collects amber fossils" for m in store.memories())


@pytest.mark.parametrize("job", ["learn", "reflect"])
@pytest.mark.parametrize("revocation", ["toggle", "forget"])
def test_revocation_discards_inflight_provider_response(bot, job, revocation):
    store = bot.S
    for i in range(4):
        store.add_message("user", f"amber fossils {i}")
    original = store.add_memory("preserve this existing fact")
    started, release, timed_out = threading.Event(), threading.Event(), threading.Event()
    errors = []

    def delayed_model(*_args, **_kwargs):
        started.set()
        if not release.wait(5):
            timed_out.set()
        return {"add": [{"text": "Collects amber fossils"}], "delete": [original],
                "style": {"tone": "stale style"}, "mood": "stale mood", "summary": "stale summary",
                "journal": "stale journal", "checkin": "stale checkin"}

    def work():
        try:
            if job == "learn":
                bot.learn("I collect amber fossils.", "Understood.")
            else:
                bot.reflect(force=True)
        except Exception as e:
            errors.append(e)

    bot.llm = delayed_model
    worker = threading.Thread(target=work)
    worker.start()
    try:
        assert started.wait(5)
        if revocation == "toggle":
            bot.update_profile({"memory_on": False})
            bot.update_profile({"memory_on": True})
        else:
            bot.forget_all()
    finally:
        release.set()
        worker.join(5)

    assert not worker.is_alive() and not timed_out.is_set() and not errors
    assert [m["text"] for m in store.memories()] == (["preserve this existing fact"] if revocation == "toggle" else [])
    assert not store.journal()
    for key in ("style", "mood", "summary", "checkin"):
        assert "stale" not in str(store.get(key))
    assert "stale" not in str(bot.state["last_ops"])
