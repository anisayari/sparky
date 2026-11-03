import io
import os
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import pytest

import catalog
import config
import updater


def test_interrupted_join_reuses_intact_segments(tmp_path, monkeypatch):
    data = bytes(range(120))
    requests = []

    def response(request, timeout):
        first, last = map(int, request.headers["Range"][6:].split("-"))
        requests.append((first, last))
        stream = io.BytesIO(data[first:last + 1])
        stream.status = 206
        stream.headers = {"Content-Range": f"bytes {first}-{last}/{len(data)}"}
        return stream

    monkeypatch.setattr(catalog, "SEGMENT_MIN", 100)
    monkeypatch.setattr(catalog.urllib.request, "urlopen", response)
    part = tmp_path / "model.gguf.part"
    copy = shutil.copyfileobj

    def interrupted(src, dest, length):
        if dest.tell() > 0:
            raise OSError("interrupted join")
        copy(src, dest, length)

    with monkeypatch.context() as patch:
        patch.setattr(catalog.shutil, "copyfileobj", interrupted)
        with pytest.raises(OSError, match="interrupted join"):
            catalog.fetch("https://example.invalid/model", str(part), len(data), lambda _: None)
    first = data[:20]
    assert part.read_bytes() == first
    assert part.with_suffix(".part.1").exists()
    # Emulate the first segment a crash in the OLD assembler could leave behind.
    part.write_bytes(first + data[20:35])
    catalog.fetch("https://example.invalid/model", str(part), len(data), lambda _: None)
    assert part.read_bytes() == data
    assert len(requests) == catalog.SEGMENTS
    assert list(tmp_path.iterdir()) == [part]


@pytest.mark.parametrize("kind,ext", [("llm", ".gguf"), ("image", ".safetensors")])
def test_homonymous_models_keep_distinct_files_and_legacy_settings(tmp_path, monkeypatch, kind, ext):
    name = "weights" + ext
    legacy = {"id": "hf:weights", "repo": "org/old", "kind": kind, "custom": True,
              "files": [["org/old", name, str(tmp_path / name)]]}
    catalog_file = str(tmp_path / "models.json")
    config.write_json(catalog_file, [legacy])
    registered, by_id = [], {}
    monkeypatch.setattr(catalog, "CATALOG", registered)
    monkeypatch.setattr(catalog, "BY_ID", by_id)
    monkeypatch.setattr(catalog, "CUSTOM_FILE", catalog_file)
    monkeypatch.setattr(catalog, "CM", str(tmp_path))
    monkeypatch.setattr(catalog, "M", str(tmp_path))
    monkeypatch.setattr(catalog, "P", str(tmp_path))
    monkeypatch.setattr(catalog, "install", lambda _: None)
    monkeypatch.setattr(catalog, "hf_files", lambda *_: {"mmproj": "mm.gguf", "files": [
        {"path": n, "size_gb": 2} for n in (name, "folder/" + name)]})
    catalog.load_custom()
    mids = [catalog.hf_install(repo, path, kind) for repo, path in (
        ("org/old", name), ("org/new", name), ("org/other", name), ("org/new", "folder/" + name))]
    assert mids[0] == legacy["id"]
    assert len(set(mids)) == 4 and len(registered) == 4
    assert len({m["files"][0][2] for m in registered}) == 4
    assert by_id[mids[0]]["files"][0][2] == str(tmp_path / name)
    if kind == "image":
        assert all(m["ckpt"] == os.path.basename(m["files"][0][2]) for m in registered[1:])
    else:
        assert len({m["files"][1][2] for m in registered[1:]}) == 3
    registered.clear()
    by_id.clear()
    catalog.load_custom()
    assert catalog.hf_install("org/new", name, kind) == mids[1]
    assert len(registered) == 4


def test_failed_dependencies_can_be_repaired_even_offline(tmp_path, monkeypatch):
    (tmp_path / "VERSION").write_text("1.0.0")
    (tmp_path / "requirements.txt").write_text("test-dependency")
    monkeypatch.setattr(config, "ROOT", str(tmp_path))
    monkeypatch.setattr(updater, "is_git", lambda: True)
    monkeypatch.setattr(updater, "local_changes", lambda: [])
    monkeypatch.setattr(updater, "_restart", lambda: None)
    monkeypatch.setattr(updater, "state", {"latest": "1.1.0", "updating": False, "log": ""})
    pulls = []

    def pull(*args, **kwargs):
        pulls.append(args)
        (tmp_path / "VERSION").write_text("1.1.0")
        return 0, "ok"

    monkeypatch.setattr(updater, "_git", pull)
    outcomes = iter([1, 0])
    monkeypatch.setattr(updater.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=next(outcomes), stderr="error", stdout=""))
    assert not updater.update()[0]
    updater.state["latest"] = None  # process restarted before a remote version check
    assert updater.status()["available"] and updater.status()["latest"] == "1.1.0"
    assert updater.update()[0]
    assert pulls == [("pull", "--ff-only")]
    assert not updater.status()["available"]


def test_launcher_repair_retries_dependencies_before_server_imports(tmp_path, monkeypatch):
    (tmp_path / "VERSION").write_text("1.6.2")
    (tmp_path / "requirements.txt").write_text("example-package")
    monkeypatch.setattr(config, "ROOT", str(tmp_path))
    monkeypatch.setattr(updater, "is_git", lambda: True)
    monkeypatch.setattr(updater, "local_changes", lambda: [])
    marker = Path(updater._pending_file())
    config.write_json(str(marker), {"base": "1.6.1", "target": "1.6.2"})  # crash directly after pull
    results = iter([1, 0])
    calls = []

    def pip(cmd, **kwargs):
        calls.append(cmd)
        return SimpleNamespace(returncode=next(results), stderr="offline", stdout="")

    monkeypatch.setattr(updater.subprocess, "run", pip)
    assert not updater.repair_on_start()[0] and marker.is_file()
    assert updater.repair_on_start()[0] and not marker.exists()
    assert updater.repair_on_start()[0] and len(calls) == 2
    assert calls[0] == calls[1] and calls[0][-1] == str(tmp_path / "requirements.txt")
    script = (Path(__file__).resolve().parents[1] / "start.ps1").read_text()
    assert script.index("--repair") < script.index("Start-Process $py")


def test_zip_updates_only_release_sources_and_preflights_conflicts(tmp_path):
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if not powershell:
        pytest.skip("PowerShell not installed")
    helper = Path(__file__).resolve().parents[1] / "sync-zip.ps1"
    old, release, dest = [tmp_path / x for x in ("old", "release", "install")]
    for where, version in [(old, "1.6.1"), (release, "1.6.2"), (dest, "1.6.1")]:
        (where / "app").mkdir(parents=True)
        (where / "app/server.py").write_text(version)
        (where / "VERSION").write_text(version)
    for where in (old, dest):
        (where / "app/removed.py").write_text("removed upstream")
    (release / "app/new.py").write_text("new source")
    (dest / "app/custom.py").write_text("mine")
    for name in ("config.json", "data/chats.json", "models/m.gguf", "runtime/python.exe"):
        file = dest / name
        file.parent.mkdir(exist_ok=True)
        file.write_text("keep me")
        unwanted = release / name
        unwanted.parent.mkdir(exist_ok=True)
        unwanted.write_text("must not ship")

    def sync(*args):
        # Windows PowerShell and pwsh have separate built-in module paths.
        env = {k: v for k, v in os.environ.items() if k.upper() != "PSMODULEPATH"}
        return subprocess.run([powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(helper),
                               "-Source", str(release), "-Destination", str(dest), *args], env=env, capture_output=True, text=True, timeout=25)

    (dest / "app/server.py").write_text("locally modified")
    assert sync("-Baseline", str(old)).returncode != 0
    assert (dest / "VERSION").read_text() == "1.6.1" and not (dest / "app/new.py").exists()
    assert (dest / "app/server.py").read_text() == "locally modified"
    (dest / "app/server.py").write_text("1.6.1")
    result = sync("-Baseline", str(old))
    assert result.returncode == 0, result.stderr
    assert (dest / "app/server.py").read_text() == "1.6.2" and not (dest / "app/removed.py").exists()
    assert (dest / "app/new.py").read_text() == "new source" and (dest / "app/custom.py").read_text() == "mine"
    assert all((dest / name).read_text() == "keep me" for name in ("config.json", "data/chats.json", "models/m.gguf", "runtime/python.exe"))
    (release / "VERSION").write_text("1.6.3")
    assert sync().returncode == 0 and (dest / "VERSION").read_text() == "1.6.3"
