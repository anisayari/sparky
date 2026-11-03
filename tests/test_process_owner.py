"""Process ownership regressions. No tests terminate a live OS process."""
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import time
from unittest.mock import Mock

import pytest

import process_owner


def test_foreign_listener_survives_all_engine_actions(monkeypatch):
    import server
    monkeypatch.setattr(server, "_engines", process_owner.Engines())
    monkeypatch.setattr(server.psutil, "net_connections", Mock(side_effect=AssertionError("looking up port owners")))
    monkeypatch.setattr(server.psutil, "Process", Mock(side_effect=AssertionError("opening process by PID")))
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        monkeypatch.setattr(server.config, "LLM_PORT", port)
        monkeypatch.setattr(server.config, "COMFY_PORT", port)
        # Inspect the real listener locally; this regression needs no outgoing TCP permission.
        monkeypatch.setattr(server, "port_listening", lambda p: p == port and listener.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN))
        server.stop_llm()
        server.restart_comfy()
        with pytest.raises(RuntimeError, match="port"):
            server.launch_llm({})
        assert server.port_listening(port)


def test_restart_and_shutdown_use_only_the_original_child_objects(monkeypatch):
    first, second, comfy = (Mock() for _ in range(3))
    for proc in (first, second, comfy):
        proc.running.return_value = True
        proc.pid = 1234  # Reuse must have no bearing on the target.
    factory = Mock(side_effect=[first, second, comfy])
    monkeypatch.setattr(process_owner, "Engine", factory)
    engines = process_owner.Engines()
    assert engines.spawn("llm", ["llama"], "log")
    assert not engines.spawn("llm", ["llama"], "log")
    first.running.return_value = False
    assert engines.spawn("llm", ["llama"], "log")
    first.stop.assert_called_once()
    second.stop.assert_not_called()
    engines.spawn("comfy", ["python"], "log")
    engines.stop("llm")
    engines.stop("llm")
    second.stop.assert_called_once()
    comfy.stop.assert_not_called()
    engines.close()
    comfy.stop.assert_called_once()
    with pytest.raises(RuntimeError):
        engines.spawn("llm", ["llama"], "log")
    assert factory.call_count == 3


@pytest.mark.skipif(os.name != "nt", reason="Windows kernel jobs")
def test_engine_exits_naturally_inside_its_job(tmp_path):
    log = tmp_path / "engine.log"
    engine = process_owner.Engine([sys.executable, "-I", "-S", "-c", "print('entered engine')"], log, str(tmp_path))
    assert engine.process.wait(timeout=15) == 0, log.read_text()
    assert log.read_text().strip() == "entered engine"
    engine.stop()  # Releases the empty job after the engine and its launcher have exited.


@pytest.mark.skipif(os.name != "nt", reason="Windows kernel jobs")
def test_failed_job_assignment_never_executes_the_engine(monkeypatch):
    call = Mock()
    close = Mock()
    monkeypatch.setattr(process_owner, "open_job", lambda *args: 17)
    monkeypatch.setattr(process_owner, "join_job", lambda *args: False)
    monkeypatch.setattr(process_owner, "close_handle", close)
    monkeypatch.setattr(process_owner.subprocess, "call", call)
    with pytest.raises(OSError):
        process_owner._run_in_job("synthetic-job", ["synthetic-engine"])
    close.assert_called_once_with(17)
    call.assert_not_called()


@pytest.mark.skipif(os.name != "nt", reason="Windows named stop signal")
def test_stop_script_signals_only_its_installation_and_waits(tmp_path):
    script = pathlib.Path(__file__).resolve().parents[1] / "stop.ps1"
    script = shutil.copy(script, tmp_path / "stop.ps1")
    cmd = ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script)]
    with process_owner.instance(str(tmp_path / "other")) as other:
        absent = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        assert absent.returncode == 0 and "No managed" in absent.stdout, absent.stderr
        with process_owner.instance(str(tmp_path)) as requested:
            with pytest.raises(RuntimeError):
                with process_owner.instance(str(tmp_path)):
                    pytest.fail("a duplicate instance started")
            client = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            until = time.monotonic() + 15
            while not requested() and client.poll() is None and time.monotonic() < until:
                time.sleep(0.03)
            assert requested()
            assert client.poll() is None
            assert not other()
        out, err = client.communicate(timeout=15)
        assert client.returncode == 0 and "Sparky stopped." in out, err
        with process_owner.instance(str(tmp_path)) as restarted:
            assert not restarted()
