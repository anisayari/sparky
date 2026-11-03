"""The checker treats generated code as data; only computer tools can request native actions."""
import json
import os
from types import SimpleNamespace

import pytest

import checker
import computer
import harness


def test_python_checked_without_running_or_materializing_project(tmp_path, monkeypatch):
    # Run the normal harness path with the persisted automatic setting enabled.
    marker = tmp_path / "outside.txt"
    marker.write_text("unchanged")
    source = f"from pathlib import Path\nPath({str(marker)!r}).write_text('changed')"
    session = harness.Session(ephemeral=True)
    session._emit = lambda event: None
    unsafe_id = str(tmp_path)  # an older imported project's key is not a filesystem capability
    session.projects[unsafe_id] = {"title": "Sample", "entry": "main.py", "files": {"main.py": source}}
    monkeypatch.setitem(harness.SETTINGS, "autotest", True)
    events = []
    result = session.test_projects([unsafe_id], events.append, 1)[0][1]
    assert result["status"] == "passed" and result["code"] == 0
    assert events[-1]["status"] == "passed"
    assert marker.read_text() == "unchanged" and list(tmp_path.iterdir()) == [marker]
    assert "not executed" in result["output"]


def test_manual_check_uses_the_same_read_only_boundary(tmp_path, monkeypatch):
    import server
    marker = tmp_path / "sentinel"
    marker.write_text("kept")
    pid = str(tmp_path)
    project = {"entry": "main.py", "files": {"main.py": f"open({str(marker)!r}, 'w').close()"}}
    session = SimpleNamespace(projects={pid: project})
    monkeypatch.setattr(server, "get_session", lambda sid: session)
    sent = []
    handler = SimpleNamespace(body=lambda: {"id": "existing", "project": pid}, send=lambda *args: sent.append(args))
    server.Handler.projrun(handler, "/api/projrun")
    assert sent[0][0] == 200 and sent[0][1]["status"] == "passed"
    assert marker.read_text() == "kept" and list(tmp_path.iterdir()) == [marker]


def test_project_syntax_and_static_resources():
    invalid = checker.check({"files": {"main.py": "if: pass"}})
    assert invalid["status"] == "failed" and "main.py" in invalid["output"]
    project = {"entry": "web/index.html", "files": {"web/index.html": '<img src="../art/cat.png">'}}
    assert checker.check(project)["status"] == "failed"
    project["assets"] = {"art/cat.png": 3}
    assert checker.check(project)["status"] == "passed"
    project["files"]["web/index.html"] = '<base href="../"><img src="art/cat.png">'
    assert checker.check(project)["status"] == "passed"


def test_node_checker_never_runs_project_hooks_or_startup_options(tmp_path, monkeypatch):
    if not checker.node_dir():
        pytest.skip("Node.js is not installed")
    marker = tmp_path / "touched"
    origin = tmp_path / "startup.js"
    payload = f'require("fs").writeFileSync({json.dumps(str(marker))}, "touched")'
    origin.write_text(payload)
    monkeypatch.setenv("NODE_OPTIONS", f'--require "{origin}"')
    package = json.dumps({"type": "module", "scripts": {"preinstall": "node bootstrap.js", "test": "node bootstrap.js"},
                          "dependencies": {"sample": "1.0.0"}})
    project = {"entry": "main.js", "files": {"package.json": package, "main.js": "export const n = 1;",
                                             "bootstrap.js": payload, "--require.js": payload,
                                             "index.html": "<script>" + payload + "</script>"}}
    assert checker.check(project)["status"] == "passed"
    assert not marker.exists()
    project["files"]["index.html"] = "<script>const = 3;</script>"
    failure = checker.check(project)
    assert failure["status"] == "failed" and "index.html" in failure["report"]


def test_unavailable_validator_is_not_reported_as_success(monkeypatch):
    monkeypatch.setattr(checker, "node_dir", lambda: None)
    for files in ({"main.js": "const n = 1"}, {"main.txt": "text"}):
        result = checker.check({"files": files})
        assert result["status"] == "unverified" and not result["ok"] and result["code"] is None
    result = checker.check({"entry": "main.ts", "files": {"main.ts": "weird syntax", "package.json": "{}"}})
    assert result["status"] == "unverified" and "main.ts" in result["output"]

    def unavailable(_project):
        raise OSError("missing checker")
    monkeypatch.setattr(checker, "check", unavailable)
    session = harness.Session(ephemeral=True)
    session.projects["p"] = {"title": "Example", "files": {"main.py": "pass"}}
    session._emit = lambda event: None
    events = []
    session.test_projects(["p"], events.append, 1)
    assert events[-1]["status"] == "unverified" and not events[-1]["ok"]


def test_powershell_card_shows_exact_command_and_directory(tmp_path, monkeypatch):
    session = harness.Session(ephemeral=True)
    session.config["computer"] = "ask"
    turn = SimpleNamespace(computer=0, spare=lambda: None, tools_used=False)
    command = "Write-Output " + ("a" * 700) + "\nWrite-Output last"
    args = {"tool": "run", "cmd": ["powershell.exe", command], "cwd": str(tmp_path)}
    calls, approved = [], []
    monkeypatch.setattr(computer, "t_run", lambda **kwargs: calls.append(kwargs) or "ok")

    def emit(event):
        if event["type"] == "approve":
            approved.append(event["summary"])
            session.approve(event["id"], "once")

    session._on_computer(turn, args, emit)
    assert approved == [f"PowerShell · {tmp_path}\n{command}"]
    assert calls == [{"command": command, "cwd": str(tmp_path)}]

    for extra in ({"paths": ["unrelated.txt"]}, {"command": "echo something else"}, {"cmd": "echo\x1btest"}):
        session._on_computer(turn, args | extra, emit)
    assert len(approved) == len(calls) == 1


def test_large_office_file_is_refused_by_local_read(tmp_path, monkeypatch):
    import attach
    monkeypatch.setattr(attach, "MAX_BYTES", 4)
    doc = tmp_path / "sample.docx"
    doc.write_bytes(b"12345")
    with pytest.raises(ValueError, match="volumineux"):
        computer.t_read(os.fspath(doc))
