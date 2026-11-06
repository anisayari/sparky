"""Provenance, migration, explicit forget, and the reflection cursor."""
import sqlite3
import threading

import memory
from test_companion_privacy import isolated_bot as isolated_bot


def test_legacy_rows_do_not_gain_invented_sources_or_reused_ids(tmp_path, monkeypatch):
    path = str(tmp_path / "old.db")
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE memories(id INTEGER PRIMARY KEY,text TEXT,category TEXT,importance INTEGER,"
                   "created REAL,updated REAL,last_used REAL,uses INTEGER,pinned INTEGER,source TEXT,vec TEXT)")
        db.execute("INSERT INTO memories VALUES(42,'old','other',4,1,1,1,0,0,'chat',NULL)")
    monkeypatch.setattr(memory.EMB, "encode", lambda *args, **kwargs: None)
    store = memory.Store(path)
    assert store.origins(42) == {"source": "chat", "updated": 1, "messages": []}
    store.forget_memory(42)
    new = store.add_memory("new", source="user")
    assert new > 42 and not store.forget_memory(42)
    store.forget_all()
    assert store.add_memory("next") > new
    store.db.close()


def test_foreground_learning_records_the_exact_user_message(isolated_bot, monkeypatch):
    bot = isolated_bot
    finished = threading.Event()
    original = bot.learn

    def waitable(*args):
        try:
            original(*args)
        finally:
            finished.set()
    monkeypatch.setattr(bot, "learn", waitable)
    bot.llm = lambda messages, **kw: {"add": [{"text": "Collects amber fossils."}]} if kw.get("json_mode") else "Hello."
    assert bot.chat("I collect amber fossils.", lambda event: None) == "Hello."
    assert finished.wait(3)
    fact, = bot.S.memories()
    origin, = bot.S.origins(fact["id"])["messages"]
    assert origin["text"] == "I collect amber fossils." and origin["ts"] == bot.S.messages()[0]["ts"]
    bot.edit_memory({"id": fact["id"], "text": "I wrote this replacement myself."})
    assert not bot.S.origins(fact["id"])["messages"] and bot.S.origins(fact["id"])["source"] == "user"


def test_merge_preserves_sources_and_protects_pinned_or_manual_facts(isolated_bot, monkeypatch):
    bot, store = isolated_bot, isolated_bot.S
    ids = [store.add_message("user", "Amber fossils " + str(n)) for n in (1, 2)]
    facts = [store.add_memory("Amber fossils", message_id=i) for i in ids]
    manual = store.add_memory("Manual", source="user")
    pinned = store.add_memory("Pinned", pinned=1)
    monkeypatch.setattr(store, "similarity", lambda a, b: 1)
    data = {"merge": [{"ids": [*facts, *facts, manual, pinned], "text": "Collects amber fossils"}], "delete": [manual, pinned]}
    bot._apply_reflection(data, store.memories(), store.messages())
    assert [m["id"] for m in store.origins(facts[0])["messages"]] == ids
    assert store.origins(facts[1]) is None and store.origins(manual) and store.origins(pinned)


def test_forget_clears_derivatives_and_rejects_stale_learning(isolated_bot):
    bot, store = isolated_bot, isolated_bot.S
    text = "secret amber fossils"
    source = store.add_message("user", text)
    store.add_message("assistant", text)
    mid = store.add_memory(text, message_id=source)
    for key in ("summary", "checkin", "mood"):
        store.set(key, text)
    store.set("style", {"tone": text})
    store.add_journal(text)
    start, release = threading.Event(), threading.Event()

    def stale(*args, **kwargs):
        start.set()
        assert release.wait(3)
        return {"add": [{"text": text}]}
    bot.llm = stale
    job = threading.Thread(target=bot.learn, args=(text, "", bot._memory_epoch, source))
    job.start()
    try:
        assert start.wait(2)
        bot.edit_memory({"id": mid, "delete": True})
    finally:
        release.set()
        job.join(4)
    assert not job.is_alive() and not store.memories() and not bot.history()
    assert not store.journal() and not bot.reflect(force=True)
    assert text not in str(bot.system_prompt("hello"))
    assert len(store.messages()) == 2  # transcript is readable, but older context never reaches the model


def test_reflection_backlog_is_contiguous_and_invalid_json_is_retried(isolated_bot):
    bot, store = isolated_bot, isolated_bot.S
    for i in range(100):
        store.add_message("user", str(i))
    bot.llm = lambda *args, **kw: {}
    assert not bot.reflect(force=True) and not store.get("reflected_upto")
    bot.llm = lambda *args, **kw: {"summary": "ok"}
    assert bot.reflect(force=True) and store.get("reflected_upto") == 80
    assert bot.reflect(force=True) and store.get("reflected_upto") == 100


def test_extractor_can_propose_only_visible_memories_for_confirmed_removal(isolated_bot):
    bot, store = isolated_bot, isolated_bot.S
    first, other = store.add_memory("first fact"), store.add_memory("another")
    store.set("summary", "an existing summary")
    bot._apply_learning({"delete": [first, other]}, "forget that", "", related_ids={first})
    assert store.get("forget_requests") == [first]
    assert store.get("summary") and store.origins(first)
    bot.edit_memory({"id": first, "keep": True})
    assert not store.get("forget_requests") and store.origins(first)
    bot._apply_learning({"delete": [first]}, "forget that", "", related_ids={first})
    bot.edit_memory({"id": first, "delete": True})
    assert not store.get("forget_requests") and not store.get("summary") and store.origins(other)
