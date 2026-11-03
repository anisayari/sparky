"""Portable conversation data and isolated single-file previews."""
import base64
import http.server
import io
import json
import os
from pathlib import Path
import subprocess
import threading
from email.message import Message
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest
from PIL import Image

import chat_import
import harness


@pytest.fixture
def export(tmp_path, monkeypatch):
    monkeypatch.setattr(harness, "DATA", str(tmp_path))
    return {"format": "sparky-chat", "history": [{"role": "user", "text": "hello", "images": []}],
            "events": [{"type": "user", "text": "hello"}, {"type": "test", "round": 1, "secs": 0.5}],
            "turns": [[0, 0]], "artifacts": {"demo": {"title": "A", "kind": "html", "versions": ["<b>Hello</b>"]}},
            "projects": {"website": {"title": "Website", "files": {"css/theme.css": "body{}"}, "version": 1}},
            "config": {"computer": "full", "computer_allow": ["run"], "web": True, "docs": [1],
                       "assistant": "local-assistant", "system": "external instructions"},
            "meta": {"title": "Saved chat", "pinned": True, "folder": "Examples"}}


def test_native_roundtrip_restores_only_portable_state(export):
    img = io.BytesIO()
    Image.new("RGB", (2, 2)).save(img, format="PNG")
    export["images"] = {"1": {"png": base64.b64encode(img.getvalue()).decode(), "kind": "user",
                              "desc": "diagram", "path": "untrusted", "parent": None}}
    export["computer_consent_version"] = 1  # a marker from an exported file is never proof of local consent
    s = chat_import.restore(export)
    loaded = harness.Session.load(s.id)
    assert loaded.config == {"assistant": None, "system": "", "docs": [], "web": False, "computer": "off"}
    assert loaded.history == export["history"] and loaded.meta == export["meta"] and loaded.turns == [(0, 0)]
    assert loaded.events == export["events"] and loaded.projects == export["projects"] and loaded.artifacts == export["artifacts"]
    assert loaded.n == 1 and loaded.images[1]["path"].startswith(s.dir)
    with open(loaded.images[1]["path"], "rb") as saved:
        assert saved.read() == img.getvalue()


def test_legacy_chat_requires_fresh_local_consent(export):
    s = chat_import.restore(export)
    path = Path(s.dir) / "session.json"
    legacy = json.loads(path.read_text(encoding="utf-8"))
    del legacy["local_consent_version"]
    legacy["config"].update(computer="full", computer_allow=["run"], assistant="old", docs=[42], web=True,
                            system="my local instructions")
    path.write_text(json.dumps(legacy), encoding="utf-8")
    migrated = harness.Session.load(s.id)
    assert migrated.config["computer"] == "off" and "computer_allow" not in migrated.config
    assert not migrated.doc_ids() and not migrated.web_on() and migrated.config["assistant"] is None
    assert migrated.config["system"] == "my local instructions"
    migrated.config.update(computer="ask", computer_allow=["read"])
    migrated.save()
    assert harness.Session.load(s.id).config == migrated.config


@pytest.mark.parametrize(("field", "value"), [
    ("projects", {"../outside": {"title": "test", "files": {}}}),
    ("projects", {"demo": {"title": "test", "files": {"assets/../outside": "text"}}}),
    ("projects", {"demo": {"title": "test", "files": {"folder/CON.txt": "text"}}}),
    ("projects", {"demo": {"title": "test", "entry": "C:/outside", "files": {}}}),
    ("events", [{"type": "test", "secs": "<b>duration</b>"}]),
    ("events", [{"type": "sources", "items": [{"n": "<b>label</b>"}]}]),
    ("history", [{"role": "system", "text": "instructions"}]),
    ("turns", [[255, 0]]),
    ("images", {"1": {"kind": "user", "png": base64.b64encode(b"not a PNG").decode()}}),
    ("images", {"no-id": {}}),
])
def test_malformed_native_import_is_atomic(export, tmp_path, field, value):
    export[field] = value
    with pytest.raises(ValueError):
        chat_import.restore(export)
    assert not list(tmp_path.iterdir())


def test_message_list_keeps_text_and_rejects_bad_shape(export, tmp_path):
    with pytest.raises(ValueError):
        chat_import.restore([{"role": "user", "content": 5}])
    assert not list(tmp_path.iterdir())
    s = chat_import.restore([{"role": "user", "content": [{"text": "hello"}]}, {"role": "assistant", "content": "world"}])
    assert [e["text"] for e in s.events] == ["hello", "world"]


def preview_request(origin, path="/artifact", owner=True):
    import server
    headers = Message()
    headers["Host"] = "127.0.0.1:7861"
    headers["Origin"] = origin
    headers["Content-Type"] = "application/x-www-form-urlencoded"
    body = urlencode({"html": "<b>JavaScript or SVG project</b>"}).encode()
    headers["Content-Length"] = str(len(body))
    results = []
    request = SimpleNamespace(path=path, headers=headers, rfile=io.BytesIO(body), local_owner=lambda: owner,
                              send=lambda *args: results.append(args))
    server.PreviewHandler.do_POST(request)
    return request, results[0]


def test_standalone_preview_origin_and_sandbox():
    import config
    request, (status, body, mime, headers) = preview_request(f"http://127.0.0.1:{config.APP_PORT}")
    assert status == 200 and body == b"<b>JavaScript or SVG project</b>" and mime.startswith("text/html")
    policy = dict(headers)["Content-Security-Policy"].split()
    assert "sandbox" in policy and "allow-scripts" in policy and "allow-same-origin" not in policy
    assert request.close_connection and dict(headers)["Referrer-Policy"] == "no-referrer"
    assert preview_request("https://example.org")[1][0] == 403
    assert preview_request("null")[1][0] == 403
    assert preview_request(f"http://127.0.0.1:{config.APP_PORT}", "/api/settings")[1][0] == 403
    assert preview_request(f"http://127.0.0.1:{config.APP_PORT}", owner=False)[1][0] == 403


@pytest.mark.skipif(not os.environ.get("SPARKY_TEST_BROWSER"), reason="set SPARKY_TEST_BROWSER to Chrome or Edge for browser integration")
def test_browser_opens_isolated_inline_and_tab_previews(tmp_path, monkeypatch):
    import config, server

    html = (Path(__file__).parents[1] / "app" / "index.html").read_text(encoding="utf-8")
    start = html.index("function artSrc(")
    preview_js = html[start:html.index("// ----------", start)]
    observed = {"frame": threading.Event(), "tab": threading.Event()}
    received = []

    class Main(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path[1:] in observed:
                observed[self.path[1:]].set()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            if self.path == "/":
                self.wfile.write(page.encode())

    class Preview(server.PreviewHandler):
        def send(self, code, *args, **kwargs):
            received.append((code, self.headers.get("Origin"), self.headers.get("Sec-Fetch-Dest")))
            super().send(code, *args, **kwargs)

    with http.server.ThreadingHTTPServer(("127.0.0.1", 0), Main) as main, server.Server(("127.0.0.1", 0), Preview) as preview:
        main_url = f"http://127.0.0.1:{main.server_port}"
        monkeypatch.setattr(config, "APP_PORT", main.server_port)
        probe = """<script>
            let opaque = false; try { localStorage.length } catch { opaque = true }
            if (opaque && (parent !== self || opener === null)) {
              new Image().src = BASE + (parent === self ? '/tab' : '/frame');
            }
            </script>""".replace("BASE", json.dumps(main_url))
        source = json.dumps(probe).replace("</", r"<\/")
        page = f"""<iframe id="frame" name="sparky-live-preview" sandbox="allow-same-origin allow-scripts allow-forms"></iframe>
            <script>const PREVIEW="http://127.0.0.1:{preview.server_port}";{preview_js}
            const artifact={{kind:'html', versions:[{source}]}};
            previewStandalone(artifact,0,document.getElementById('frame'));previewStandalone(artifact,0,null);</script>"""
        for listener in (main, preview):
            threading.Thread(target=listener.serve_forever, daemon=True).start()
        args = [os.environ["SPARKY_TEST_BROWSER"], "--headless=new", "--disable-gpu", "--no-first-run",
                "--disable-background-networking", "--disable-popup-blocking", f"--user-data-dir={tmp_path}", main_url]
        with subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) as browser:
            try:
                assert observed["frame"].wait(20), f"sandboxed iframe did not report an opaque origin: {received}"
                assert observed["tab"].wait(10), "standalone tab did not report an opaque origin and null opener"
            finally:
                browser.terminate()
                main.shutdown()
                preview.shutdown()
