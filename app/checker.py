"""Automatic project tests: build / run each project when the model says it is done, and report the
errors precisely so the model can fix them (the harness loops until the tests pass).

- website (entry .html): JavaScript syntax (node --check), then the page is opened in headless Edge/Chrome:
  uncaught exceptions, console errors and missing project files are collected, and every button is
  clicked once to catch errors in event handlers;
- Python (entry .py): syntax of every file (py_compile), then the program runs (30 s max);
- Node.js (entry .js or package.json): npm install when there are dependencies, npm run build / test
  when those scripts exist, otherwise the program runs (a server still running after the delay = OK).
"""
import json, os, re, shutil, socket, subprocess, sys, tempfile, time, urllib.request

import config

NOWIN = config.NOWIN


def node_dir():
    local = os.path.join(config.RUNTIME, "node")
    if os.path.exists(os.path.join(local, "node.exe")):
        return local
    if os.path.exists(os.path.join(local, "bin", "node")):  # macOS / Linux tarball layout
        return os.path.join(local, "bin")
    n = shutil.which("node")
    return os.path.dirname(n) if n else None


def write_project(root, files, session=None, pid=None):
    """Writes the text files, and copies the generated images (assets) of the project."""
    import harness
    shutil.rmtree(root, ignore_errors=True)
    for fp, content in files.items():
        dest = os.path.join(root, *harness.clean_path(fp).split("/"))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "w", encoding="utf-8", newline="") as f:
            f.write(content)
    if session is not None:
        for fp in session.projects.get(pid, {}).get("assets", {}):
            src = session.asset_file(pid, fp)
            if src and os.path.exists(src):
                dest = os.path.join(root, *harness.clean_path(fp).split("/"))
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                shutil.copyfile(src, dest)


def run(cmd, cwd, timeout):
    """(exit code, output, timed_out). The PATH includes the bundled Node.js."""
    env = os.environ | {"PYTHONIOENCODING": "utf-8", "NO_COLOR": "1", "FORCE_COLOR": "0", "CI": "1"}
    nd = node_dir()
    if nd:
        env["PATH"] = nd + os.pathsep + env.get("PATH", "")
    try:
        p = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                             errors="replace", stdin=subprocess.DEVNULL, creationflags=NOWIN, env=env, shell=False)
    except FileNotFoundError as e:
        return 127, str(e), False
    try:
        out, err = p.communicate(timeout=timeout)
        timed_out = False
    except subprocess.TimeoutExpired:
        kill_tree(p.pid)  # npm.cmd -> cmd.exe -> node.exe: killing only the first one leaves node running
        out, err = p.communicate()
        timed_out = True
    return (None if timed_out else p.returncode), (out or "") + (("\n" + err) if (err or "").strip() else ""), timed_out


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


def browser_path():
    for p in (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe", r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
              r"C:\Program Files\Google\Chrome\Application\chrome.exe", r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"):
        if os.path.exists(p):
            return p
    return shutil.which("msedge") or shutil.which("chrome")


LAYOUT = r"""(()=>{const W=innerWidth,H=innerHeight,out=[],src=i=>i.getAttribute('src')||'image';
  const sw=document.documentElement.scrollWidth;
  if(sw>W+8)out.push(`the page is wider than the screen (${sw}px for ${W}px): an element overflows horizontally (fixed widths, big images?)`);
  for(const i of document.images){const r=i.getBoundingClientRect();if(!r.width)continue;
    if(i.complete&&i.naturalWidth===0)out.push(`image ${src(i)} is broken (cannot be displayed)`);
    else if(i.closest('header,nav')&&r.height>H*0.6)out.push(`image ${src(i)} in the header/menu is displayed ${Math.round(r.width)}x${Math.round(r.height)}px: far too big there (a logo/banner must be constrained: height, object-fit: cover)`);
    else if(r.height>H*1.15)out.push(`image ${src(i)} is displayed ${Math.round(r.width)}x${Math.round(r.height)}px, taller than the screen: constrain it with CSS (max-height / height + object-fit: cover)`);}
  for(const h of document.querySelectorAll('h1,h2')){const r=h.getBoundingClientRect(),t=(h.textContent||'').trim();
    if(t.length>8&&r.width>0&&r.width<160)out.push(`heading "${t.slice(0,40)}" is squeezed into ${Math.round(r.width)}px of width (a neighbour element takes all the room)`);}
  return out.slice(0,8)})()"""


def browser_errors(url, wait=2.5):
    """Opens the page in a headless browser and returns the list of errors (None if no browser)."""
    exe = browser_path()
    try:
        import websocket
    except ImportError:
        return None
    if not exe:
        return None
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    prof = tempfile.mkdtemp(prefix="sparky-check-")
    proc = subprocess.Popen([exe, "--headless=new", f"--remote-debugging-port={port}", f"--user-data-dir={prof}", "--no-first-run",
                             "--disable-gpu", "--mute-audio", "about:blank"], creationflags=NOWIN,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    errors, seen = [], set()

    def add(msg):
        msg = msg.strip()[:600]
        if msg and msg not in seen:
            seen.add(msg)
            errors.append(msg)
    try:
        page = None
        for _ in range(60):
            try:
                page = next(t for t in json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=2)) if t["type"] == "page")
                break
            except Exception:
                time.sleep(0.2)
        if not page:
            return None
        ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=10, suppress_origin=True)
        ids = iter(range(1, 10**6))

        def send(method, **params):
            ws.send(json.dumps({"id": next(ids), "method": method, "params": params}))
        for m in ("Runtime.enable", "Log.enable", "Network.enable", "Page.enable"):
            send(m)
        send("Page.navigate", url=url)
        base = url.rsplit("/", 1)[0] + "/"

        def pump(until):
            while time.time() < until:
                ws.settimeout(max(0.05, until - time.time()))
                try:
                    m = json.loads(ws.recv())
                except Exception:
                    continue
                meth, p = m.get("method"), m.get("params", {})
                if meth == "Runtime.exceptionThrown":
                    d = p.get("exceptionDetails", {})
                    desc = (d.get("exception") or {}).get("description") or d.get("text", "")
                    where = f' ({d.get("url", "").replace(base, "")}:{d.get("lineNumber", 0) + 1})' if d.get("url") else ""
                    add("Uncaught " + desc.split("\n    at ")[0] + where)
                elif meth == "Runtime.consoleAPICalled" and p.get("type") in ("error", "assert"):
                    add("console.error: " + " ".join(str(a.get("value", a.get("description", ""))) for a in p.get("args", [])))
                elif meth == "Log.entryAdded" and p.get("entry", {}).get("level") == "error":
                    e = p["entry"]
                    if not e.get("url") or e["url"].startswith(base):  # external resources (fonts, CDN) do not count
                        add(e.get("text", "") + (f' ({e["url"].replace(base, "")})' if e.get("url") else ""))
                elif meth == "Network.responseReceived":
                    r = p.get("response", {})
                    if r.get("status", 200) >= 400 and r.get("url", "").startswith(base):
                        add(f'Missing file {r["url"].replace(base, "")} (HTTP {r["status"]})')
        pump(time.time() + wait)
        # layout check, measured in the rendered page (1280x800): what "runs without errors" does not catch
        send("Emulation.setDeviceMetricsOverride", width=1280, height=800, deviceScaleFactor=1, mobile=False)
        pump(time.time() + 0.5)
        lid = next(ids)
        ws.send(json.dumps({"id": lid, "method": "Runtime.evaluate", "params": {"returnByValue": True, "expression": LAYOUT}}))
        end = time.time() + 5
        while time.time() < end:
            ws.settimeout(max(0.05, end - time.time()))
            try:
                m = json.loads(ws.recv())
            except Exception:
                continue
            if m.get("id") == lid:
                for msg in (m.get("result", {}).get("result", {}).get("value") or []):
                    add("Layout: " + msg)
                break
        # smoke test: every button / clickable element once (dialogs disabled, links not followed)
        send("Runtime.evaluate", expression="""(()=>{window.alert=window.confirm=window.prompt=()=>true;
            const els=[...document.querySelectorAll('button,[onclick],input[type=button],input[type=submit],[role=button]')].slice(0,40);
            for(const el of els){try{el.click()}catch(e){console.error(String(e))}} return els.length})()""")
        pump(time.time() + 1.5)
        ws.close()
        return errors
    finally:
        kill_tree(proc.pid)  # the browser and its helper processes
        shutil.rmtree(prof, ignore_errors=True)


def hint_paths(problem, files, assets):
    """A missing file that exists elsewhere in the project (wrong relative path): spell out the exact fix."""
    m = re.search(r"Missing file ([^\s]+)", problem)
    if not m:
        return problem
    missing = m.group(1)
    name = missing.rsplit("/", 1)[-1]
    real = next((f for f in list(files) + list(assets) if f.rsplit("/", 1)[-1] == name and f != missing), None)
    if not real:
        return problem
    users = [f for f, c in files.items() if name in c]
    fixes = []
    for u in users:
        depth = u.count("/")
        rel = "../" * depth + real
        fixes.append(f'in {u}, the path must be "{rel}" (relative to {u}), e.g. url({rel})' if u.endswith(".css")
                     else f'in {u}, the path must be "{rel}"')
    return problem + f'\n  -> the file exists at "{real}": ' + "; ".join(fixes or [f'use the path "{real}"']) + "."


def check(session, pid, project, app_port):
    """Tests one project. Returns {ok, kind, report (for the model), output (for the console), code, secs}."""
    files, entry = project["files"], project.get("entry") or ""
    if not files:
        return {"ok": False, "kind": "empty", "output": "No code file in this project yet.", "code": 1, "secs": 0,
                "report": "The project has pictures but no code file yet: write index.html (and its CSS/JS files) "
                          "as artifacts with the SAME \"project\" id and a \"path\"."}
    root = os.path.join(session.dir, "projects", pid)
    write_project(root, files, session, pid)
    t0 = time.time()
    problems, out = [], []
    nd = node_dir()
    exe = "node.exe" if os.name == "nt" else "node"
    node = os.path.join(nd, exe) if nd and os.path.exists(os.path.join(nd, exe)) else (shutil.which("node") if nd else None)
    npm = os.path.join(nd, "npm.cmd" if os.name == "nt" else "npm") if nd else None
    kind = "web" if entry.endswith((".html", ".htm")) else "python" if entry.endswith(".py") else \
        "node" if entry.endswith((".js", ".mjs", ".cjs")) or "package.json" in files else "other"

    if kind == "web":
        if node:
            for fp in files:
                if fp.endswith((".js", ".mjs")):
                    code, o, _ = run([node, "--check", fp], root, 20)
                    if code:
                        problems.append(f"Syntax error in {fp}:\n{o.strip()[-1500:]}")
        errs = browser_errors(f"http://127.0.0.1:{app_port}/api/proj/{session.id}/{pid}/{entry}")
        if errs is None:
            out.append("(no headless browser available: only the JavaScript syntax was checked)")
        else:
            problems += [f"In the browser: {e}" for e in errs]
            out.append(f"Page opened in a headless browser: {len(errs)} error(s).")
        code = 1 if problems else 0
    elif kind == "python":
        for fp in files:
            if fp.endswith(".py"):
                c, o, _ = run([sys.executable, "-X", "utf8", "-m", "py_compile", fp], root, 20)
                if c:
                    problems.append(f"Syntax error in {fp}:\n{o.strip()[-1500:]}")
        code = 1
        if not problems:
            code, o, timed = run([sys.executable, "-X", "utf8", entry], root, 30)
            out.append(o)
            if timed:
                problems.append("The program was still running after 30 s (infinite loop, or waiting for input()?). "
                                "It must finish on its own and print its results.")
            elif code:
                problems.append(f"python {entry} exited with code {code}:\n{o.strip()[-2500:]}")
    elif kind == "node":
        if not node:
            return {"ok": True, "kind": kind, "report": "", "output": "Node.js is not installed: test skipped.", "code": None, "secs": 0}
        pkg = {}
        if "package.json" in files:
            try:
                pkg = json.loads(files["package.json"])
            except ValueError as e:
                problems.append(f"package.json is not valid JSON: {e}")
        if pkg.get("dependencies") or pkg.get("devDependencies"):
            c, o, timed = run([npm, "install", "--no-audit", "--no-fund", "--loglevel=error"], root, 240)
            out.append("$ npm install\n" + o)
            if c or timed:
                problems.append("npm install failed:\n" + o.strip()[-2000:])
        scripts = pkg.get("scripts", {})
        code = 0
        if not problems:
            for s in ("build", "test"):
                if s in scripts and "no test specified" not in scripts[s]:
                    c, o, timed = run([npm, "run", s], root, 120)
                    out.append(f"$ npm run {s}\n" + o)
                    if c or timed:
                        problems.append(f"npm run {s} failed:\n" + o.strip()[-2500:])
            if not problems:
                main = entry if entry.endswith((".js", ".mjs", ".cjs")) else pkg.get("main") or next((f for f in ("index.js", "main.js", "server.js", "app.js") if f in files), None)
                if main:
                    c, o, timed = run([node, main], root, 15)
                    out.append(f"$ node {main}\n" + o)
                    if timed and not re.search(r"\b(Error|Exception)\b", o):
                        out.append("(still running after 15 s: treated as a server that started correctly)")
                    elif c:
                        problems.append(f"node {main} exited with code {c}:\n{o.strip()[-2500:]}")
        code = 1 if problems else 0
    else:
        return {"ok": True, "kind": kind, "report": "", "output": "Nothing to run for this project.", "code": None, "secs": 0}

    problems = [hint_paths(p, files, project.get("assets", {})) for p in problems]
    return {"ok": not problems, "kind": kind, "report": "\n\n".join(problems)[:5000],
            "output": ("\n".join(o for o in out if o).strip() + ("\n\n" + "\n\n".join(problems) if problems else ""))[-20000:],
            "code": 0 if not problems else (code or 1), "secs": round(time.time() - t0, 1)}
