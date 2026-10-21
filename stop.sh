#!/bin/bash
# Stops Sparky: UI, assistant (llama-server) and image engine (ComfyUI).
ROOT="$(cd "$(dirname "$0")" && pwd)"
cfg() { [ -f "$ROOT/config.json" ] && plutil -extract "$1" raw -o - "$ROOT/config.json" 2>/dev/null; }  # plutil reads JSON too
for key in app_port:7860 llm_port:8080 comfy_port:8188; do
  p="$(cfg "${key%%:*}")"; p="${p:-${key##*:}}"
  pids="$(lsof -nP -t -iTCP:"$p" -sTCP:LISTEN 2>/dev/null)"
  [ -n "$pids" ] && kill $pids 2>/dev/null
done
echo "Sparky stopped."
