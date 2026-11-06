"""Cooperative cancellation and context budgeting for a single chat turn."""
import contextlib
import contextvars
import queue
import socket
import threading
from collections import deque


class TurnFeed:
    """A short, read-only tail of the current turn. Old cursors recover from the authoritative transcript."""

    def __init__(self):
        self.changed = threading.Condition()
        self.turn_id = None
        self.running = False
        self.cursor = 0
        self.tail = deque(maxlen=48)
        self.partial = None

    def begin(self, turn_id):
        with self.changed:
            self.turn_id = turn_id
            self.cursor = 0
            self.tail.clear()
            self.partial = None
            self.running = True
            self.changed.notify_all()

    def publish(self, event, persistent=False):
        with self.changed:
            self.cursor += 1
            self.tail.append((self.cursor, event))
            self.partial = None if persistent else event
            if event["type"] == "done":
                self.running = False
                self.partial = None
                # Release transient token snapshots as soon as the transcript is complete.
                self.tail.clear()
                self.tail.append((self.cursor, event))
            self.changed.notify_all()

    def poll(self, turn_id, after, timeout=12):
        with self.changed:
            self.changed.wait_for(lambda: (turn_id != self.turn_id or after != self.cursor or not self.running), timeout)
            if turn_id != self.turn_id or after > self.cursor or (self.tail and after < self.tail[0][0] - 1):
                return {"reset": True}
            return {"cursor": self.cursor, "running": self.running, "events": [e for seq, e in self.tail if seq > after]}


class Cancelled(Exception):
    pass


local = threading.local()
_transport = contextvars.ContextVar("sparky_stream_transport", default=None)


class Transport:
    def __init__(self):
        self.lock = threading.Lock()
        self.closed = False
        self.stop = None

    def close(self):
        with self.lock:
            self.closed = True
            stop = self.stop
        if stop is not None:
            with contextlib.suppress(Exception):
                stop()


@contextlib.contextmanager
def response(handle):
    """Unblock an active SSE socket on cancellation or early end of a JSON reply."""
    transport = _transport.get()
    if transport is None:
        yield
        return
    sock = getattr(getattr(getattr(handle, "fp", None), "raw", None), "_sock", None)
    with transport.lock:
        if transport.closed:
            raise Cancelled()
        transport.stop = (lambda: sock.shutdown(socket.SHUT_RDWR)) if sock is not None else handle.close
    try:
        yield
    finally:
        with transport.lock:
            transport.stop = None


def check():
    token = getattr(local, "token", None)
    if token is not None and token.is_set():
        raise Cancelled("Génération arrêtée.")


def stream(factory):
    """Wake the turn even if the provider is waiting for its first token."""
    token = getattr(local, "token", None)
    if token is None:
        yield from factory()
        return
    channel = queue.Queue(maxsize=8)
    closed = threading.Event()
    transport = Transport()

    def put(item):
        while not closed.is_set() and not token.is_set():
            try:
                channel.put(item, timeout=.1)
                return True
            except queue.Full:
                continue
        return False

    def receive():
        current = _transport.set(transport)
        try:
            with contextlib.closing(factory()) as source:
                for item in source:
                    if not put(("data", item)):
                        return
        except Exception as exc:
            put(("error", exc))
        finally:
            _transport.reset(current)
            put(("end", None))

    check()
    ctx = contextvars.copy_context()
    threading.Thread(target=ctx.run, args=(receive,), daemon=True).start()
    try:
        while True:
            check()
            try:
                kind, value = channel.get(timeout=.1)
            except queue.Empty:
                continue
            if kind == "end":
                return
            if kind == "error":
                raise value
            yield value
    finally:
        closed.set()
        transport.close()


def text_cost(text):
    # Conservative estimate across technical text and non-Latin alphabets.
    return (len(text.encode("utf-8")) + 2) // 3


def message_cost(message):
    body = message["content"]
    if isinstance(body, str):
        return 5 + text_cost(body)
    return 5 + sum(text_cost(part.get("text", "")) if part.get("type") == "text" else 1100 for part in body)


def clip(text, tokens):
    if text_cost(text) <= tokens:
        return text
    mark = "\n[… contexte abrégé …]\n"
    size = max(0, 3 * tokens - len(mark.encode()))
    raw = text.encode()
    return raw[:size * 3 // 4].decode(errors="ignore") + mark + (
        raw[-(size // 4):].decode(errors="ignore") if size >= 4 else "")


def fit(messages, window, max_output, anchor=None):
    """Keep the system instructions and newest request, then fit a contiguous suffix of history."""
    window = max(1024, int(window))
    total = sum(message_cost(m) for m in messages)
    available = window - 128
    output = min(max_output, max(256, available - total))
    if total + output <= available:
        return messages, output
    output = min(max_output, max(256, min(window // 5, available - message_cost(messages[0]) - 160)))
    room = available - output
    system = dict(messages[0])
    system["content"] = clip(system["content"], max(128, room - max(128, min(room // 2, message_cost(messages[-1])))))
    if len(messages) == 1:
        return [system], output
    budget = max(32, room - message_cost(system) - 5)
    request = None
    if anchor and anchor < len(messages) - 1:
        source = messages[anchor]["content"]
        text = source if isinstance(source, str) else "\n".join(p["text"] for p in source if p.get("type") == "text")
        request = messages[anchor] | {"content": clip(text, max(16, budget // 3 - 5))}
        budget -= message_cost(request)
    latest = dict(messages[-1])
    if message_cost(latest) > budget:
        if isinstance(latest["content"], str):
            latest["content"] = clip(latest["content"], budget - 6)
        else:
            parts = []
            for part in latest["content"]:
                if part.get("type") == "text" and budget >= 16:
                    body = clip(part["text"], budget - 5)
                    parts.append({"type": "text", "text": body})
                    budget -= text_cost(body)
                elif part.get("type") == "image_url" and budget >= 1100:
                    parts.append(part)
                    budget -= 1100
            latest["content"] = parts
    remaining = room - message_cost(system) - message_cost(latest) - (message_cost(request) if request else 0)
    suffix = []
    for msg in reversed(messages[anchor + 1 if request else 1:-1]):
        cost = message_cost(msg)
        if cost > remaining:
            break
        suffix.append(msg)
        remaining -= cost
    return [system, *([request] if request else []), *reversed(suffix), latest], output
