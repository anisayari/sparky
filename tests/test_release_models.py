import hashlib
from pathlib import Path
import sys
import time
from unittest.mock import Mock

import pytest

import catalog
import config
import process_owner


def test_split_model_keeps_all_parts_and_ignores_incomplete_groups(tmp_path, monkeypatch):
    names = [f"folder/model-{n:05d}-of-00012.gguf" for n in range(1, 13)]
    files = [{"rfilename": n, "size": 2**30} for n in names + ["bad-00001-of-00002.gguf"]]
    monkeypatch.setattr(catalog, "hf_get", lambda _: {"siblings": files})
    monkeypatch.setattr(catalog, "indicators", lambda _: {})
    monkeypatch.setattr(catalog, "M", str(tmp_path))
    monkeypatch.setattr(catalog, "CATALOG", [])
    monkeypatch.setattr(catalog, "BY_ID", {})
    monkeypatch.setattr(catalog, "save_custom", lambda: None)
    monkeypatch.setattr(catalog, "install", lambda _: None)
    assert catalog.hf_files("org/test", "llm")["files"] == [{"path": names[0], "size_gb": 12, "shards": names}]
    mid = catalog.hf_install("org/test", names[0], "llm")
    entry = catalog.BY_ID[mid]
    parts = [Path(f[2]) for f in entry["files"] if f]
    for i, path in enumerate(parts, 1):
        assert path.name.endswith(f"-{i:05d}-of-00012.gguf")
        path.write_bytes(b"GGUF")
    assert catalog.installed(entry) and catalog.llm_args(entry) == [str(parts[0])]
    parts[-1].unlink()
    assert not catalog.installed(entry)


def test_weight_download_pins_revision_and_rejects_same_size_corruption(tmp_path, monkeypatch):
    content = b"one valid unit"
    checksum = hashlib.sha256(content).hexdigest()
    revision = "a" * 40
    dest = tmp_path / "weights.gguf"
    monkeypatch.setattr(catalog, "hf_get", lambda _: {
        "sha": revision, "siblings": [{"rfilename": "weight.gguf", "size": len(content), "lfs": {"sha256": checksum}}]})
    monkeypatch.setattr(catalog, "BY_ID", {"test": {"files": [("org/test", "weight.gguf", str(dest))]}})
    job = {}
    monkeypatch.setattr(catalog, "jobs", {"test": job})
    chunks = iter([b"x" * len(content), content])

    def fetch(url, part, size, report):
        assert f"/resolve/{revision}/weight.gguf" in url and size == len(content)
        assert not Path(part).exists()
        Path(part).write_bytes(next(chunks))

    monkeypatch.setattr(catalog, "fetch", fetch)
    Path(str(dest) + ".part").write_bytes(b"from old revision")
    catalog._install("test")
    assert job["state"] == "error" and not dest.exists()
    catalog._install("test")
    assert job["state"] == "ok" and dest.read_bytes() == content
    assert catalog._matches(dest, len(content), hashlib.sha1(f"blob {len(content)}\0".encode() + content).hexdigest())


def test_unowned_engine_is_never_sent_prompts(monkeypatch):
    import harness
    import server
    foreign = f"http://127.0.0.1:{config.LLM_PORT}"
    monkeypatch.setattr(process_owner, "_managed", process_owner.Engines())
    monkeypatch.setattr(config, "EXTERNAL_LLM", False)
    outgoing = Mock(side_effect=AssertionError("unowned service received an HTTP request"))
    monkeypatch.setattr(harness.urllib.request, "urlopen", outgoing)
    assert not server.alive(foreign + "/health")
    with pytest.raises(ConnectionError):
        next(harness.llama_deltas({"messages": []}))
    with pytest.raises(ConnectionError):
        harness.http_json(foreign + "/v1/chat/completions", {})
    outgoing.assert_not_called()
    monkeypatch.setattr(config, "EXTERNAL_LLM", True)
    process_owner.require_endpoint(foreign + "/v1/chat/completions")


def test_insufficient_space_stops_before_receiving_weights(tmp_path, monkeypatch):
    meta = [("https://example.invalid/sha/weights", "weights", str(tmp_path / "weights"), "a" * 64, 128)]
    job = {}
    fetch = Mock(side_effect=AssertionError("ran download without enough space"))
    monkeypatch.setattr(catalog, "BY_ID", {"test": {"files": [("org/test", "weights", str(tmp_path / "weights"))]}})
    monkeypatch.setattr(catalog, "jobs", {"test": job})
    monkeypatch.setattr(catalog, "_remote_files", lambda _: meta)
    monkeypatch.setattr(catalog, "SEGMENT_MIN", 100)
    monkeypatch.setattr(catalog.shutil, "disk_usage", lambda _: Mock(free=200))
    monkeypatch.setattr(catalog, "fetch", fetch)
    catalog._install("test")
    assert job["state"] == "error" and "disque" in job["error"]
    fetch.assert_not_called()


def test_running_engine_recognizes_its_own_listener(tmp_path):
    engines = process_owner.Engines()
    log = tmp_path / "listener.log"
    script = ("import socket,time; s=socket.socket(); s.bind(('127.0.0.1',0)); "
              "s.listen(); print(s.getsockname()[1],flush=True); time.sleep(30)")
    try:
        assert engines.spawn("llm", [sys.executable, "-I", "-c", script], log)
        deadline = time.monotonic() + 10
        while (not log.exists() or not log.read_text().strip()) and time.monotonic() < deadline:
            time.sleep(.05)
        port = int(log.read_text().strip())
        assert engines.owns_listener("llm", port)
        assert not engines.owns_listener("comfy", port)
        engines.stop("llm")
        assert not engines.owns_listener("llm", port)
    finally:
        engines.close()
