"""Minimal Model Context Protocol client: plugs the user's MCP servers (tools only) into the assistant.

data/mcp.json uses the Claude Desktop format:
  {"mcpServers": {"files": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "C:\\docs"],
                            "env": {}, "disabled": false},
                  "remote": {"url": "https://example.com/mcp", "headers": {"Authorization": "Bearer ..."}}}}
Local servers speak JSON-RPC 2.0 over stdio (one message per line); "url" servers use the Streamable
HTTP transport. Tools are exposed as "server__tool".
"""
import collections, itertools, json, os, shutil, subprocess, threading, urllib.error, urllib.request

import config

FILE = os.path.join(config.DATA, "mcp.json")
PROTOCOL = "2025-06-18"
CLIENT = {"name": "Sparky", "version": "1.0"}
SEP = "__"
START_TIMEOUT = 60   # npx may download the package on first launch


class McpError(RuntimeError):
    pass


def load():
    cfg = config.read_json(FILE, {}) or {}
    if not isinstance(cfg.get("mcpServers"), dict):
        cfg["mcpServers"] = {}
    return cfg


def save(cfg):
    specs = cfg.get("mcpServers")
    if not isinstance(specs, dict) or any(not isinstance(spec, dict) for spec in specs.values()):
        raise ValueError("invalid MCP server configuration")
    with _lock:
        config.write_json(FILE, cfg)
        retired = _detach_obsolete(specs if enabled() else {})
    _stop_async(retired)


def _env(spec):
    """Child environment: ours + the portable Node (runtime\\node) first in PATH + the server's env."""
    env = dict(os.environ)
    try:
        import checker
        nd = checker.node_dir()
    except Exception:
        nd = None
    if nd:
        env["PATH"] = nd + os.pathsep + env.get("PATH", "")
    env.update({k: str(v) for k, v in (spec.get("env") or {}).items()})
    return env


class Server:
    def __init__(self, name, spec):
        self.name, self.spec = name, dict(spec)
        self.state, self.error, self.tools = "off", "", []
        self.proc = self.session = None
        self.ids = itertools.count(1)
        self.pending = {}   # id -> [Event, message]
        self.lock = threading.Lock()
        self.proc_lock = threading.Lock()
        self.stderr = collections.deque(maxlen=20)
        self.stopping = False

    # ---------------------------------------------------------------- lifecycle
    def start(self):
        if self.stopping:
            return
        self.state, self.error, self.tools = "starting", "", []
        threading.Thread(target=self._run, daemon=True, name=f"mcp-{self.name}").start()

    def _run(self):
        try:
            if self.stopping:
                return
            if not self.spec.get("url"):
                self._spawn()
            self.request("initialize", {"protocolVersion": PROTOCOL, "capabilities": {}, "clientInfo": CLIENT},
                         START_TIMEOUT)
            self.notify("notifications/initialized")
            tools, cursor = [], None
            while True:
                r = self.request("tools/list", {"cursor": cursor} if cursor else {}, 30)
                tools += r.get("tools") or []
                cursor = r.get("nextCursor")
                if not cursor:
                    break
            self.tools = [{"name": t["name"], "description": t.get("description") or "",
                           "inputSchema": t.get("inputSchema") or {"type": "object"}} for t in tools if t.get("name")]
            if not self.stopping:
                self.state = "ready"
        except Exception as e:
            if not self.stopping:
                self._fail(e)

    def _fail(self, e):
        tail = " | ".join(self.stderr)
        self.state, self.error = "error", f"{e}" + (f" ({tail[-500:]})" if tail else "")
        self._kill()

    def stop(self):
        self.stopping = True
        if self.spec.get("url") and self.session:
            try:   # ends the session on the server (optional in the spec)
                req = urllib.request.Request(self.spec["url"], method="DELETE", headers=self._headers())
                urllib.request.urlopen(req, timeout=3).close()
            except (OSError, ValueError):
                pass
        self._kill()
        self.state, self.tools, self.session = "off", [], None

    def _kill(self):
        with self.proc_lock:
            p, self.proc = self.proc, None
        self._kill_process(p)
        for ev in list(self.pending.values()):
            ev[1] = {"error": {"code": -32000, "message": "server stopped"}}
            ev[0].set()

    @staticmethod
    def _kill_process(p):
        if p and p.poll() is None:
            if os.name == "nt":   # npx -> cmd -> node: the whole tree must go
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], capture_output=True,
                               creationflags=config.NOWIN)
            else:
                p.terminate()
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                p.kill()

    # ---------------------------------------------------------------- stdio transport
    def _spawn(self):
        cmd = self.spec.get("command")
        if not cmd:
            raise McpError("no command or url")
        env = _env(self.spec)
        exe = shutil.which(cmd, path=env.get("PATH")) or cmd   # npx -> npx.cmd on Windows
        if self.stopping:
            raise McpError("server stopped")
        proc = subprocess.Popen([exe] + [str(a) for a in self.spec.get("args") or []], env=env,
                                cwd=self.spec.get("cwd") or None, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, creationflags=config.NOWIN)
        with self.proc_lock:
            if not self.stopping:
                self.proc = proc
                threading.Thread(target=self._read, args=(proc,), daemon=True).start()
                threading.Thread(target=self._drain, args=(proc,), daemon=True).start()
                return
        # stop() may have completed before Popen returned. Only this local handle owns that child.
        self._kill_process(proc)
        raise McpError("server stopped")

    def _read(self, proc):
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                self._dispatch(json.loads(line))
            except ValueError:
                self.stderr.append(line[:200].decode("utf-8", "replace"))   # stray log on stdout
        if not self.stopping and proc is self.proc:
            self._fail(McpError(f"process exited (code {proc.poll()})"))

    def _drain(self, proc):
        for line in proc.stderr:
            line = line.decode("utf-8", "replace").strip()
            if line:
                self.stderr.append(line[:300])

    def _write(self, msg):
        p = self.proc
        if not p or p.poll() is not None:
            raise McpError("server is not running")
        with self.lock:
            if self.stopping:
                raise McpError("server stopped")
            p.stdin.write(json.dumps(msg, ensure_ascii=False).encode() + b"\n")
            p.stdin.flush()

    def _dispatch(self, msg):
        if isinstance(msg, list):
            for m in msg:
                self._dispatch(m)
            return
        if not isinstance(msg, dict):
            return
        if "method" in msg:
            if "id" in msg:   # server -> client request: we offer no capability (ping is required though)
                reply = {"jsonrpc": "2.0", "id": msg["id"]}
                if msg["method"] == "ping":
                    reply["result"] = {}
                else:
                    reply["error"] = {"code": -32601, "message": f"method not found: {msg['method']}"}
                try:
                    self._send(reply)
                except (OSError, McpError):
                    pass
            return   # notifications are ignored
        ev = self.pending.get(msg.get("id"))
        if ev:
            ev[1] = msg
            ev[0].set()

    # ---------------------------------------------------------------- Streamable HTTP transport
    def _headers(self):
        h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
             "MCP-Protocol-Version": PROTOCOL, **{k: str(v) for k, v in (self.spec.get("headers") or {}).items()}}
        if self.session:
            h["Mcp-Session-Id"] = self.session
        return h

    def _post(self, msg, timeout=60):
        req = urllib.request.Request(self.spec["url"], data=json.dumps(msg).encode(), headers=self._headers())
        try:
            r = urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            raise McpError(f"HTTP {e.code}: {e.read(300).decode('utf-8', 'replace')}") from None
        with r:
            if self.stopping:
                raise McpError("server stopped")
            self.session = r.headers.get("Mcp-Session-Id") or self.session
            if r.status == 202:
                return
            if r.headers.get_content_type() == "text/event-stream":
                data = []
                for line in r:   # stops once our answer arrived (the stream may stay open)
                    line = line.decode("utf-8", "replace").rstrip("\r\n")
                    if line.startswith("data:"):
                        data.append(line[5:].lstrip())
                    elif not line and data:
                        self._dispatch(json.loads("\n".join(data)))
                        data = []
                        if "id" not in msg or self.pending.get(msg["id"], [None, None])[1]:
                            return
                if data:
                    self._dispatch(json.loads("\n".join(data)))
            else:
                body = r.read()
                if body.strip():
                    self._dispatch(json.loads(body))

    def _send(self, msg, timeout=60):
        if self.stopping:
            raise McpError("server stopped")
        if self.spec.get("url"):
            self._post(msg, timeout)
        else:
            self._write(msg)

    # ---------------------------------------------------------------- JSON-RPC
    def request(self, method, params=None, timeout=60):
        if self.stopping:
            raise McpError("server stopped")
        rid = next(self.ids)
        ev = self.pending[rid] = [threading.Event(), None]
        try:
            self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}, timeout)
            if not ev[0].wait(timeout):
                raise McpError(f"{method}: no answer after {timeout} s")
        finally:
            self.pending.pop(rid, None)
        msg = ev[1] or {}
        if "error" in msg:
            raise McpError(f"{method}: {msg['error'].get('message', msg['error'])}")
        return msg.get("result") or {}

    def notify(self, method, params=None):
        self._send({"jsonrpc": "2.0", "method": method, **({"params": params} if params else {})})


# ---------------------------------------------------------------- registry
SERVERS = {}
_lock = threading.Lock()


def enabled():
    import harness
    return bool(harness.SETTINGS.get("mcp_on", True))


def _stop_async(peers):
    for peer in peers:
        threading.Thread(target=peer.stop, daemon=True).start()


def set_enabled(on):
    import harness
    with _lock:
        if on == enabled():
            return
        harness.SETTINGS["mcp_on"] = on
        retired = [] if on else _detach_obsolete({})
    _stop_async(retired)
    if on:
        threading.Thread(target=start_all, daemon=True).start()


def _detach_obsolete(cfg):
    """Called under the registry lock. Retire identities synchronously, shut down transports outside the lock."""
    retired = []
    for name, peer in list(SERVERS.items()):
        spec = cfg.get(name)
        if (not isinstance(spec, dict) or spec.get("disabled") or
                {k: v for k, v in spec.items() if k != "disabled"} != {k: v for k, v in peer.spec.items() if k != "disabled"}):
            peer.stopping = True
            retired.append(SERVERS.pop(name))
    return retired


def start_all():
    """Starts every enabled server of mcp.json in the background; stops removed, disabled or changed ones."""
    with _lock:
        cfg = load()["mcpServers"] if enabled() else {}
        retired = _detach_obsolete(cfg)
        for name, spec in cfg.items():
            if name not in SERVERS and isinstance(spec, dict) and not spec.get("disabled"):
                SERVERS[name] = Server(name, spec)
                SERVERS[name].start()
    for peer in retired:
        peer.stop()


def restart(name):
    with _lock:
        spec = load()["mcpServers"].get(name)
        old = SERVERS.pop(name, None)
        if old:
            old.stopping = True
        if enabled() and spec and not spec.get("disabled"):
            SERVERS[name] = Server(name, spec)
            SERVERS[name].start()
    if old:
        old.stop()


def stop_all():
    with _lock:
        retired = _detach_obsolete({})
    for peer in retired:
        peer.stop()


def status():
    """[{name, state: off|starting|ready|error, error, tools}] for every server of mcp.json."""
    out = []
    for name, spec in load()["mcpServers"].items():
        s = SERVERS.get(name)
        if s and not (spec or {}).get("disabled"):
            out.append({"name": name, "state": s.state, "error": s.error, "tools": list(s.tools)})
        else:
            out.append({"name": name, "state": "off", "error": "", "tools": []})
    return out


def wait_ready(timeout=START_TIMEOUT):
    """Waits until no server is still starting (tests, or before the first message)."""
    ev = threading.Event()
    for _ in range(int(timeout * 10)):
        if not any(s.state == "starting" for s in list(SERVERS.values())):
            return True
        ev.wait(0.1)
    return False


def tools():
    """Flat list of ready tools: [{name: "server__tool", server, tool, description, inputSchema}]."""
    with _lock:
        if not enabled():
            return []
        return [{"name": f"{s.name}{SEP}{t['name']}", "server": s.name, "tool": t["name"],
                 "description": t["description"], "inputSchema": t["inputSchema"]}
                for s in SERVERS.values() if s.state == "ready" for t in s.tools]


def _content(result):
    parts = []
    for c in result.get("content") or []:
        kind = c.get("type")
        if kind == "text":
            parts.append(c.get("text", ""))
        elif kind in ("image", "audio"):
            parts.append(f"[{kind} {c.get('mimeType', '')}, {len(c.get('data') or '') * 3 // 4} bytes]")
        elif kind == "resource":
            r = c.get("resource") or {}
            parts.append(r["text"] if "text" in r else f"[resource {r.get('uri', '')} {r.get('mimeType', '')}]")
        elif kind == "resource_link":
            parts.append(f"[resource link {c.get('uri', '')}" + (f" — {c['name']}" if c.get("name") else "") + "]")
    if not parts and result.get("structuredContent") is not None:
        parts.append(json.dumps(result["structuredContent"], ensure_ascii=False))
    text = "\n".join(p for p in parts if p)
    return ("Error: " + text) if result.get("isError") else text


def call(qualified_name, arguments=None, timeout=60):
    """Runs a tool ("server__tool") and returns its result as text (raises McpError when unavailable)."""
    with _lock:
        if not enabled():
            raise McpError("MCP is disabled")
        # Qualified names can themselves contain the separator. Require exactly one advertised match.
        matches = [(s, t["name"]) for s in SERVERS.values() if s.state == "ready" for t in s.tools
                   if f"{s.name}{SEP}{t['name']}" == qualified_name]
        if len(matches) != 1:
            raise McpError(f"unknown tool {qualified_name}")
        s, tool = matches[0]
    response = s.request("tools/call", {"name": tool, "arguments": arguments or {}}, timeout)
    with _lock:
        if not enabled() or SERVERS.get(s.name) is not s:
            raise McpError("MCP authorization changed during the call")
    return _content(response)


def _compact(schema):
    """{"a": "number", "b?": "string"}-style summary of a JSON schema (enough for a small model)."""
    props, req = (schema or {}).get("properties") or {}, set((schema or {}).get("required") or [])
    out = {}
    for k, v in props.items():
        t = v.get("type") or ("enum" if "enum" in v else "any")
        if isinstance(t, list):
            t = "|".join(t)
        if "enum" in v:
            t = "|".join(json.dumps(e) for e in v["enum"][:8])
        elif t == "array" and isinstance(v.get("items"), dict):
            t = (v["items"].get("type") or "any") + "[]"
        out[k if k in req else k + "?"] = t
    return json.dumps(out, ensure_ascii=False)


def tools_prompt():
    """Short system-prompt text listing the MCP tools ('' when none is ready)."""
    ts = tools()
    if not ts:
        return ""
    lines = ["MCP tools available (arguments: ? = optional):"]
    for t in ts:
        d = " ".join(t["description"].split())
        lines.append(f"- {t['name']}: {d[:200] + ('...' if len(d) > 200 else '')} args {_compact(t['inputSchema'])}")
    return "\n".join(lines)
