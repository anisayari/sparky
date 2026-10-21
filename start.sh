#!/bin/bash
# Starts Sparky (server + ComfyUI + assistant) and opens the UI in the browser.
ROOT="$(cd "$(dirname "$0")" && pwd)"
cfg() { [ -f "$ROOT/config.json" ] && plutil -extract "$1" raw -o - "$ROOT/config.json" 2>/dev/null; }  # plutil reads JSON too
PORT="$(cfg app_port)"; PORT="${PORT:-7860}"
PY="$(cfg python)"; PY="${PY:-$ROOT/runtime/venv/bin/python}"
if [ ! -x "$PY" ]; then
  osascript -e "display alert \"Sparky\" message \"Python not found ($PY). Run install.sh first.\"" >/dev/null 2>&1 || echo "Python not found ($PY). Run install.sh first."
  exit 1
fi
up() { nc -z 127.0.0.1 "$PORT" >/dev/null 2>&1; }
if ! up; then
  mkdir -p "$ROOT/data"
  cd "$ROOT" && nohup "$PY" app/server.py --port "$PORT" >"$ROOT/data/server.log" 2>"$ROOT/data/server.err.log" </dev/null &
  for _ in $(seq 60); do up && break; sleep 0.5; done
fi
open "http://127.0.0.1:$PORT"
