"""Static checks for untrusted projects. Project code is never written, imported or run here."""
import json
import os
import posixpath
import shutil
import subprocess
import sys
import time
from html.parser import HTMLParser
from urllib.parse import quote, unquote, urljoin, urlsplit

import config

MAX_FILES = 128
MAX_CHARS = 2_000_000
CHECK_TIMEOUT = 8
MAX_SECONDS = 25


def node_dir():
    local = os.path.join(config.RUNTIME, "node")
    if os.path.isfile(os.path.join(local, "node.exe")):
        return local
    node = shutil.which("node.exe" if os.name == "nt" else "node")
    return os.path.dirname(node) if node else None


def run(cmd, cwd, timeout, source=None):
    """Run a trusted checker with a time limit. Source is supplied only on stdin."""
    # NODE_OPTIONS, NODE_PATH and other user startup hooks must not participate in a check.
    env = {name: os.environ[name] for name in ("SystemRoot", "WINDIR", "TMP", "TEMP") if name in os.environ}
    with subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.PIPE if source is not None else subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                          creationflags=config.NOWIN, env=env) as proc:
        try:
            out, err = proc.communicate(source, timeout=timeout)
        except subprocess.TimeoutExpired:
            kill_tree(proc.pid)
            out, err = proc.communicate()
            return None, out + err, True
    return proc.returncode, out + err, False


def kill_tree(pid):
    import psutil
    try:
        parent = psutil.Process(pid)
        procs = parent.children(recursive=True) + [parent]
    except psutil.Error:
        return
    for proc in procs:
        try:
            proc.kill()
        except psutil.Error:
            pass
    psutil.wait_procs(procs, timeout=5)


class PageSources(HTMLParser):
    """Collect static resource URLs and inline JavaScript without rendering the page."""
    def __init__(self):
        super().__init__()
        self.resources = []
        self.scripts = []
        self.script_mode = None
        self.base_href = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "base" and self.base_href is None:
            self.base_href = attrs.get("href")
        if tag == "script":
            mime = (attrs.get("type") or "").lower().split(";")[0].strip()
            if "src" not in attrs and mime in ("", "module", "text/javascript", "application/javascript"):
                self.script_mode = "module" if mime == "module" else "commonjs"
        if tag in ("script", "img") and attrs.get("src"):
            self.resources.append(attrs["src"])
        if tag == "link" and "stylesheet" in (attrs.get("rel") or "").lower().split() and attrs.get("href"):
            self.resources.append(attrs["href"])

    def handle_data(self, data):
        if self.script_mode and data.strip():
            self.scripts.append((data, self.script_mode))

    def handle_endtag(self, tag):
        if tag == "script":
            self.script_mode = None


def result(status, kind, message, started):
    code = {"passed": 0, "failed": 1, "unverified": None}[status]
    return {"status": status, "ok": status == "passed", "kind": kind, "code": code,
            "report": message[:5000], "output": message[:20000], "secs": round(time.monotonic() - started, 1)}


def check(project):
    """Check in-memory source, never using a project-controlled cwd, command, or filename."""
    started = time.monotonic()
    files = project.get("files")
    if not isinstance(files, dict) or not files:
        return result("failed", "empty", "The project has no text files.", started)
    if (len(files) > MAX_FILES or any(not isinstance(k, str) or not isinstance(v, str) for k, v in files.items())
            or sum(len(v) for v in files.values()) > MAX_CHARS):
        return result("unverified", "static", "Project exceeds the static check limits or contains invalid files.", started)
    entry = project.get("entry")
    if entry and (not isinstance(entry, str) or entry not in files):
        return result("failed", "static", "The project entry is missing from its files.", started)
    assets = project.get("assets") or {}
    if not isinstance(assets, dict):
        return result("unverified", "static", "Invalid project assets.", started)
    available = set(files) | set(assets)
    problems, skipped = [], []
    if entry and posixpath.splitext(entry)[1].lower() not in (".py", ".js", ".mjs", ".cjs", ".htm", ".html", ".json"):
        skipped.append(f"No static validator for entry {entry}.")
    units = []
    checked = 0
    try:
        for name, content in files.items():
            extension = posixpath.splitext(name)[1].lower()
            if extension == ".json":
                try:
                    json.loads(content)
                    checked += 1
                except ValueError as e:
                    problems.append(f"{name}: {e}")
            elif extension == ".py":
                units.append((name, content, "python"))
            elif extension in (".js", ".mjs", ".cjs"):
                units.append((name, content, {".mjs": "module", ".cjs": "commonjs", ".js": "auto"}[extension]))
            elif extension in (".html", ".htm"):
                page = PageSources()
                page.feed(content)
                page.close()
                base = urljoin("https://project.invalid/" + quote(name), page.base_href or "")
                for url in page.resources:
                    parts = urlsplit(urljoin(base, url))
                    if parts.netloc == "project.invalid" and not url.startswith(("/", "#", "?")):
                        target = posixpath.normpath(unquote(parts.path)).lstrip("/")
                        if target not in available:
                            problems.append(f"{name}: missing local resource {url}")
                checked += 1
                units.extend((name, script, mode) for script, mode in page.scripts)
        node = node_dir() if any(mode != "python" for _, _, mode in units) else None
        for name, content, mode in units:
            remaining = MAX_SECONDS - (time.monotonic() - started)
            if remaining <= 0:
                skipped.append("Project check time limit reached.")
                break
            if mode == "python":
                cmd = [sys.executable, "-I", "-S", "-B", "-X", "utf8", "-c",
                       "import sys; compile(sys.stdin.buffer.read().decode('utf-8'), '<project>', 'exec')"]
            elif node:
                exe = os.path.join(node, "node.exe" if os.name == "nt" else "node")
                cmd = [exe] + ([] if mode == "auto" else ["--input-type=" + mode]) + ["--check", "-"]
            else:
                if not any("Node.js" in reason for reason in skipped):
                    skipped.append("Node.js is unavailable; JavaScript syntax could not be checked.")
                continue
            code, output, timed_out = run(cmd, config.APP, min(remaining, CHECK_TIMEOUT), source=content)
            # Generic .js files can be ES modules (browser import, package type) or classic scripts.
            if mode == "auto" and code:
                remaining = MAX_SECONDS - (time.monotonic() - started)
                if remaining <= 0:
                    skipped.append("Project check time limit reached.")
                    break
                code, output, timed_out = run([cmd[0], "--input-type=module", "--check", "-"], config.APP,
                                              min(remaining, CHECK_TIMEOUT), source=content)
            if timed_out:
                skipped.append(f"{name}: syntax check timed out.")
            elif code:
                problems.append(f"{name}: {output.strip()[-1500:]}")
            else:
                checked += 1
    except (OSError, ValueError, RecursionError, AssertionError) as e:
        skipped.append(f"Static checker unavailable: {e}")
    if problems:
        return result("failed", "static", "\n".join(problems + skipped), started)
    if not checked or skipped:
        return result("unverified", "static", "\n".join(skipped) or "No supported static check for these files.", started)
    return result("passed", "static", f"{checked} static check(s) passed. Project code was not executed.", started)
