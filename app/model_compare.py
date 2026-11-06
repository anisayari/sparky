"""Run comparisons one local GGUF at a time, independently of the user's active assistant."""
import contextlib
import http.client
import json
import os
import secrets
import socket
import threading
import time

import catalog
import config
import process_owner
import turn_runtime

_slot = threading.Lock()
engines = process_owner.Engines()


def _messages(history):
    if not isinstance(history, list) or not 1 <= len(history) <= 81 or len(history) % 2 != 1:
        raise ValueError("Conversation de comparaison invalide.")
    messages = [{"role": "system", "content": "Answer the user directly, in the user's language."}]
    for i, item in enumerate(history):
        role = "assistant" if i % 2 else "user"
        if (not isinstance(item, dict) or item.get("role") != role or not isinstance(item.get("content"), str)
                or not 1 <= len(item["content"].strip()) <= 32000):
            raise ValueError("Message de comparaison invalide.")
        messages.append({"role": role, "content": item["content"]})
    return turn_runtime.fit(messages, 4096, 768)


def _request(port, key, path, payload=None, timeout=2):
    # Direct loopback connection; never follow redirects or consult an HTTP proxy.
    with contextlib.closing(http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)) as conn:
        conn.connect()
        if not engines.owns_listener("comparison", port):
            raise ValueError("Le port de comparaison n’appartient plus à ce moteur.")
        conn.auto_open = False
        body = json.dumps(payload).encode() if payload is not None else None
        conn.request("POST" if body else "GET", path, body, {"Content-Type": "application/json", "Authorization": "Bearer " + key})
        with conn.getresponse() as response:
            if response.status != 200:
                raise ValueError(f"Moteur local indisponible (HTTP {response.status}).")
            return json.load(response)


def answer(model, history):
    entry = catalog.BY_ID.get(model) if isinstance(model, str) else None
    if not entry or entry.get("cloud") or entry["kind"] != "llm" or not catalog.installed(entry):
        raise ValueError("Choisis un modèle de chat local installé.")
    messages, budget = _messages(history)
    if not _slot.acquire(blocking=False):
        raise ValueError("Une autre comparaison est déjà en cours.")
    try:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        key = secrets.token_hex(32)
        cmd = [config.LLAMA_SERVER, "-m", catalog.llm_args(entry)[0], "--host", "127.0.0.1", "--port", str(port),
               "--api-key", key, "-ngl", "0", "-c", "4096", "--jinja"]
        try:
            if not engines.spawn("comparison", cmd, os.devnull):
                raise ValueError("Moteur de comparaison occupé.")
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                child = engines.children.get("comparison")
                if child is None or not child.running():
                    raise ValueError("Impossible de charger ce modèle en mémoire.")
                if engines.owns_listener("comparison", port):
                    try:
                        if _request(port, key, "/health").get("status") == "ok":
                            break
                    except (OSError, ValueError, http.client.HTTPException):
                        pass
                time.sleep(.3)
            else:
                raise ValueError("Chargement du modèle trop long.")
            if not engines.owns_listener("comparison", port):
                raise ValueError("Moteur de comparaison arrêté.")
            start = time.monotonic()
            result = _request(port, key, "/v1/chat/completions", {"messages": messages, "max_tokens": budget,
                              "temperature": 0.2, "chat_template_kwargs": {"enable_thinking": False}}, timeout=600)
            reply = (result["choices"][0]["message"].get("content") or "").strip()
            if not reply:
                raise ValueError("Le modèle local a renvoyé une réponse vide.")
            return {"model": model, "reply": reply, "seconds": round(time.monotonic() - start, 2)}
        finally:
            engines.stop("comparison")
    finally:
        _slot.release()


def shutdown():
    with engines.lock:
        engines.closed = True
        engines.stop("comparison")
