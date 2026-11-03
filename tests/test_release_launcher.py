import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import urllib.request

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(os.name != "nt", reason="Windows release launcher")
def test_clean_archive_launch_stop_and_foreign_preview_port(tmp_path):
    # Ship only the public application. A new machine never inherits the developer's data or settings.
    source = tmp_path / "release"
    dest = tmp_path / "installed app"
    shutil.copytree(ROOT / "app", source / "app", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for filename in ("start.ps1", "stop.ps1", "VERSION"):
        shutil.copy(ROOT / filename, source)
    powershell = ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File"]
    env = {k: v for k, v in os.environ.items() if k.upper() != "PSMODULEPATH"}
    cfg = tmp_path / "custom config.json"
    env.update(SPARKY_CONFIG=str(cfg), SPARKY_VERSION_URL="http://127.0.0.1:9/VERSION")

    def run(script, *args, timeout=120):
        # A background process may inherit the shell's pipe handles on Windows.
        # Use files so the parent's diagnostic stream can be read as soon as it exits.
        with tempfile.TemporaryFile() as log:
            result = subprocess.run([*powershell, str(script), *args], env=env, stdout=log, stderr=log, timeout=timeout)
            log.seek(0)
            result.stderr = log.read().decode(errors="replace")
            return result

    sync = run(ROOT / "sync-zip.ps1", "-Source", str(source), "-Destination", str(dest))
    assert sync.returncode == 0, sync.stderr
    # Bind the preview port so the launch must fail before it opens the browser.
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1] - 1
        with socket.socket() as neighbour:
            try:
                neighbour.bind(("127.0.0.1", port))
            except OSError:
                pytest.skip("could not reserve adjacent ports")
        cfg.write_text(json.dumps({"app_port": port, "python": sys.executable}), encoding="utf-8")
        started = run(dest / "start.ps1", "-NoBrowser", "-NonInteractive")
        assert started.returncode != 0 and "already occupied" in started.stderr
    try:
        started = run(dest / "start.ps1", "-NoBrowser", "-NonInteractive")
        assert started.returncode == 0, started.stderr
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/instance", timeout=10) as response:
            identity = json.load(response)
        assert identity["instance"] == json.loads((dest / "data/instance.json").read_text())["instance"]
        assert run(dest / "start.ps1", "-NoBrowser", "-NonInteractive").returncode == 0
    finally:
        stopped = run(dest / "stop.ps1")
        assert stopped.returncode == 0, stopped.stderr
