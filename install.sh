#!/bin/bash
# Sparky - installer (macOS on Apple Silicon)
#
#   From the project folder : ./install.sh
#   One line                : curl -fsSL https://raw.githubusercontent.com/anisayari/sparky/main/install.sh | bash
#
# Options (or environment variables for the one-line install):
#   --lang en|fr     / SPARKY_LANG      interface and installer language
#   --dir <folder>   / SPARKY_DIR       install folder (default: ~/Sparky)
#   --models         / SPARKY_MODELS    also download the recommended models from the terminal
#                                       (by default they are chosen in the browser at first launch)
#   --yes            / SPARKY_YES       answer yes to every question (English unless --lang)
#   --no-launch      / SPARKY_NOLAUNCH  do not open Sparky at the end
#
# Written for the bash 3.2 that ships with macOS (no associative arrays).
set -u

REPO='https://github.com/anisayari/sparky'
LANG_UI="${SPARKY_LANG:-}"; DIR="${SPARKY_DIR:-}"
MODELS="${SPARKY_MODELS:-}"; YES="${SPARKY_YES:-}"; NOLAUNCH="${SPARKY_NOLAUNCH:-}"
while [ $# -gt 0 ]; do
  case "$1" in
    --lang) LANG_UI="${2:-}"; shift ;;
    --dir) DIR="${2:-}"; shift ;;
    --models) MODELS=1 ;;
    --yes|-y) YES=1 ;;
    --no-launch) NOLAUNCH=1 ;;
    -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1"; exit 2 ;;
  esac
  shift
done
# "curl | bash": stdin is the script itself, questions are read from the terminal
INTERACTIVE=1
if [ -n "$YES" ]; then INTERACTIVE=''; elif [ ! -t 0 ]; then
  if (exec </dev/tty) 2>/dev/null; then exec </dev/tty; else INTERACTIVE=''; fi
fi

# ------------------------------------------------------------------ look & feel
if [ -t 1 ]; then
  C_M=$'\033[35m'; C_C=$'\033[36m'; C_G=$'\033[32m'; C_Y=$'\033[33m'; C_R=$'\033[31m'; C_D=$'\033[90m'; C_B=$'\033[1m'; C_0=$'\033[0m'
else C_M=''; C_C=''; C_G=''; C_Y=''; C_R=''; C_D=''; C_B=''; C_0=''; fi
STEP=0; TOTAL=7
LOG="${TMPDIR:-/tmp}/sparky-install.log"; : >"$LOG"

step() { STEP=$((STEP + 1)); printf '\n  %s●%s %sStep %s/%s%s   %s%s%s\n' "$C_M" "$C_0" "$C_D" "$STEP" "$TOTAL" "$C_0" "$C_B" "$1" "$C_0"; }
ok() { printf '    %s✔%s %s\n' "$C_G" "$C_0" "$1"; }
info() { printf '      %s%s%s\n' "$C_D" "$1" "$C_0"; }
warn() { printf '    %s! %s%s\n' "$C_Y" "$1" "$C_0"; }
fail() {
  printf '    %s✖ %s%s\n' "$C_R" "$1" "$C_0"
  if [ -s "$LOG" ]; then echo; tail -n 12 "$LOG" | sed 's/^/      /'; echo; info "$(t logat) $LOG"; fi
  exit 1
}
ask() {  # ask "question" default(y|n)
  [ -z "$INTERACTIVE" ] && { [ "$2" = y ]; return; }
  local hint; if [ "$2" = y ]; then hint="$(t yn)"; else hint="$(t ny)"; fi
  printf '    %s› %s%s %s%s%s ' "$C_C" "$1" "$C_0" "$C_D" "$hint" "$C_0"
  local r; read -r r; r="$(printf '%s' "$r" | tr '[:upper:]' '[:lower:]')"
  [ -z "$r" ] && { [ "$2" = y ]; return; }
  case "$r" in y|yes|o|oui) return 0 ;; *) return 1 ;; esac
}
# run a program in the background with a spinner; its output goes to the log
run() {  # run "label" cmd args...
  local label="$1"; shift
  local t0=$SECONDS i=0 spin=(⠋ ⠙ ⠹ ⠸ ⠼ ⠴ ⠦ ⠧ ⠇ ⠏)  # an array: bash 3.2 slices strings by bytes
  echo "=== $label: $*" >>"$LOG"
  "$@" >>"$LOG" 2>&1 &
  local pid=$!
  while kill -0 "$pid" 2>/dev/null; do
    printf '\r    %s%s %s   %ss%s ' "$C_C" "${spin[$((i % 10))]}" "$label" "$((SECONDS - t0))" "$C_0"
    sleep 0.1; i=$((i + 1))
  done
  wait "$pid"; local code=$?
  printf '\r\033[K'
  [ $code -ne 0 ] && fail "$label (code $code)"
  printf '    %s✔%s %s   %s%ss%s\n' "$C_G" "$C_0" "$label" "$C_D" "$((SECONDS - t0))" "$C_0"
}
fetch() {  # fetch "label" url file   (curl draws its own progress bar)
  printf '    %s%s%s\n' "$C_C" "$1" "$C_0"
  curl -fL --retry 5 --progress-bar -o "$3" "$2" || fail "$(t dlfail) $2"
  ok "$1   $(( $(stat -f %z "$3") / 1048576 )) MB"
}

# ------------------------------------------------------------------ texts
t() {
  if [ "$L" = fr ]; then
    case "$1" in
      yn) echo '[O/n]' ;; ny) echo '[o/N]' ;; logat) echo 'Journal complet :' ;; dlfail) echo 'Échec du téléchargement :' ;;
      welcome1) echo 'Chat, vision, code et dictée vocale,' ;; welcome2) echo 'entièrement sur ce Mac. Rien n’en sort.' ;;
      s1) echo 'Vérification de ta machine' ;; s2) echo 'Fichiers de Sparky' ;; s3) echo 'Python' ;;
      s4) echo 'PyTorch + ComfyUI' ;; s5) echo 'Moteur IA (llama.cpp Metal)' ;; s6) echo 'Analyse du système' ;; s7) echo 'Application Sparky' ;;
      notmac) echo 'Cet installeur est pour macOS. Sous Windows, utilise install.ps1.' ;;
      intel) echo 'Mac Intel : pas d’accélération Metal pour l’IA, l’assistant tournera sur le processeur (lent).' ;;
      oldmac) echo 'macOS 13 (Ventura) ou plus récent est conseillé.' ;;
      folder) echo 'Dossier d’installation' ;; folderq) echo 'Entrée pour garder, ou tape un autre chemin :' ;;
      dlcode) echo 'Téléchargement de Sparky' ;; updated) echo 'Mise à jour de Sparky' ;; free) echo 'libres' ;;
      lowdisk) echo 'Peu d’espace disque : prévois au moins 25 Go pour les modèles.' ;;
      nopy) echo 'Python 3.10 à 3.13 introuvable.' ;; pybrew) echo 'Installation de Python 3.12 (Homebrew)' ;;
      pyhow) echo 'Installe Python 3.12 (https://www.python.org/downloads/ ou brew install python@3.12), puis relance.' ;;
      venv) echo 'Création de l’environnement Python' ;; torch) echo 'Installation de PyTorch (~1 Go)' ;; deps) echo 'Installation des dépendances' ;;
      mpsok) echo 'GPU Apple (Metal) utilisable par PyTorch' ;; nompx) echo 'PyTorch sans accélération Metal' ;;
      already) echo 'déjà installé' ;; scan) echo 'Analyse du système' ;; level) echo 'Niveau :' ;; usable) echo 'Go utilisables pour les modèles' ;;
      recommended) echo 'Recommandé pour ce Mac' ;; later) echo 'Tu choisiras et installeras tes modèles dans le navigateur, au premier lancement.' ;;
      app) echo 'Application « Sparky » dans ~/Applications (Launchpad, Spotlight)' ;; ready) echo 'Sparky est prêt !' ;;
      launchhow) echo 'Lance-le depuis Spotlight ou Launchpad (« Sparky »),' ;; launchhow2) echo 'ou ./start.sh dans le dossier d’installation.' ;;
      opening) echo 'Ouverture de Sparky dans ton navigateur...' ;; bye) echo 'Amuse-toi bien !' ;;
    esac
  else
    case "$1" in
      yn) echo '[Y/n]' ;; ny) echo '[y/N]' ;; logat) echo 'Full log:' ;; dlfail) echo 'Download failed:' ;;
      welcome1) echo 'Chat, vision, code and voice dictation,' ;; welcome2) echo 'running entirely on this Mac. Nothing leaves it.' ;;
      s1) echo 'Checking your machine' ;; s2) echo 'Sparky files' ;; s3) echo 'Python' ;;
      s4) echo 'PyTorch + ComfyUI' ;; s5) echo 'AI engine (llama.cpp Metal)' ;; s6) echo 'System scan' ;; s7) echo 'Sparky app' ;;
      notmac) echo 'This installer is for macOS. On Windows, use install.ps1.' ;;
      intel) echo 'Intel Mac: no Metal acceleration for AI, the assistant will run on the processor (slow).' ;;
      oldmac) echo 'macOS 13 (Ventura) or newer is recommended.' ;;
      folder) echo 'Install folder' ;; folderq) echo 'Enter to keep it, or type another path:' ;;
      dlcode) echo 'Downloading Sparky' ;; updated) echo 'Updating Sparky' ;; free) echo 'free' ;;
      lowdisk) echo 'Low disk space: plan at least 25 GB for the models.' ;;
      nopy) echo 'Python 3.10 to 3.13 not found.' ;; pybrew) echo 'Installing Python 3.12 (Homebrew)' ;;
      pyhow) echo 'Install Python 3.12 (https://www.python.org/downloads/ or brew install python@3.12), then run this again.' ;;
      venv) echo 'Creating the Python environment' ;; torch) echo 'Installing PyTorch (~1 GB)' ;; deps) echo 'Installing dependencies' ;;
      mpsok) echo 'Apple GPU (Metal) usable by PyTorch' ;; nompx) echo 'PyTorch without Metal acceleration' ;;
      already) echo 'already installed' ;; scan) echo 'Scanning your system' ;; level) echo 'Level:' ;; usable) echo 'GB usable for models' ;;
      recommended) echo 'Recommended for this Mac' ;; later) echo 'You will choose and install your models in the browser, at first launch.' ;;
      app) echo '"Sparky" app in ~/Applications (Launchpad, Spotlight)' ;; ready) echo 'Sparky is ready!' ;;
      launchhow) echo 'Open it from Spotlight or Launchpad ("Sparky"),' ;; launchhow2) echo 'or ./start.sh in the install folder.' ;;
      opening) echo 'Opening Sparky in your browser...' ;; bye) echo 'Have fun!' ;;
    esac
  fi
}

# ------------------------------------------------------------------ welcome + language
printf '\n   %s╭──────────────────────────────────────────────╮%s\n' "$C_M" "$C_0"
printf '   %s│%s              ✦  S P A R K Y                  %s│%s\n' "$C_M" "$C_0" "$C_M" "$C_0"
printf '   %s│%s    Your private AI assistant  -  100%% local   %s│%s\n' "$C_M" "$C_0" "$C_M" "$C_0"
printf '   %s╰──────────────────────────────────────────────╯%s\n' "$C_M" "$C_0"
L=en
case "$LANG_UI" in en|fr) L="$LANG_UI" ;; *)
  if [ -n "$INTERACTIVE" ]; then
    printf '\n    %sChoose your language / Choisis ta langue%s\n      1  English\n      2  Français\n    %s›%s ' "$C_B" "$C_0" "$C_C" "$C_0"
    read -r c; case "$c" in 2|f*|F*) L=fr ;; esac
  fi ;;
esac
GB=GB; [ "$L" = fr ] && GB=Go
printf '\n    %s\n    %s\n' "$(t welcome1)" "$(t welcome2)"

# ------------------------------------------------------------------ 1. machine
step "$(t s1)"
[ "$(uname -s)" = Darwin ] || fail "$(t notmac)"
ARCH="$(uname -m)"
OSV="$(sw_vers -productVersion)"
RAM=$(( $(sysctl -n hw.memsize) / 1073741824 ))
CHIP="$(sysctl -n machdep.cpu.brand_string)"
ok "macOS $OSV  ·  $ARCH  ·  $RAM $GB RAM"
if [ "$ARCH" = arm64 ]; then ok "$CHIP  ·  Metal"; else warn "$(t intel)"; fi
[ "${OSV%%.*}" -ge 13 ] 2>/dev/null || warn "$(t oldmac)"

# ------------------------------------------------------------------ 2. Sparky files
step "$(t s2)"
HERE=''
case "${BASH_SOURCE[0]:-}" in */*|install.sh) HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd)" ;; esac
if [ -n "$HERE" ] && [ -f "$HERE/app/server.py" ]; then
  ROOT="$HERE"
else
  if [ -z "$DIR" ]; then
    DIR="$HOME/Sparky"
    if [ -n "$INTERACTIVE" ]; then
      info "$(t folder): $DIR"
      printf '    %s› %s%s ' "$C_C" "$(t folderq)" "$C_0"
      read -r d; [ -n "$d" ] && DIR="${d/#\~/$HOME}"
    fi
  fi
  if [ -f "$DIR/app/server.py" ]; then
    [ -d "$DIR/.git" ] && run "$(t updated)" git -C "$DIR" pull --ff-only
  elif xcode-select -p >/dev/null 2>&1; then  # git without the Command Line Tools opens an install dialog
    run "$(t dlcode)" git clone --depth 1 "$REPO.git" "$DIR"
  else
    fetch "$(t dlcode)" "$REPO/archive/refs/heads/main.tar.gz" "${TMPDIR:-/tmp}/sparky.tgz"
    mkdir -p "$DIR" && tar xzf "${TMPDIR:-/tmp}/sparky.tgz" -C "$DIR" --strip-components 1 || fail "tar"
  fi
  ROOT="$(cd "$DIR" && pwd)"
fi
RUNTIME="$ROOT/runtime"
mkdir -p "$RUNTIME" "$ROOT/data"
LOG="$ROOT/data/install.log"; echo "=== Sparky install $(date '+%Y-%m-%dT%H:%M:%S')" >"$LOG"
FREE=$(( $(df -k "$ROOT" | awk 'NR==2 {print $4}') / 1048576 ))
ok "$ROOT   ·   $FREE $GB $(t free)"
[ "$FREE" -lt 25 ] && warn "$(t lowdisk)"

# ------------------------------------------------------------------ 3. Python
step "$(t s3)"
VENV="$RUNTIME/venv"; VPY="$VENV/bin/python"
pyok() { "$1" -c 'import sys; sys.exit(not ((3, 10) <= sys.version_info[:2] <= (3, 13)))' 2>/dev/null; }
if [ ! -x "$VPY" ]; then
  PY=''
  for c in python3.12 python3.11 python3.13 python3.10 /opt/homebrew/bin/python3.12 /usr/local/bin/python3.12 python3; do
    command -v "$c" >/dev/null 2>&1 && pyok "$c" && { PY="$(command -v "$c")"; break; }
  done
  if [ -z "$PY" ]; then
    BREW="$(command -v brew || ls /opt/homebrew/bin/brew 2>/dev/null)"
    [ -n "$BREW" ] || { warn "$(t nopy)"; fail "$(t pyhow)"; }
    run "$(t pybrew)" "$BREW" install python@3.12
    PY="$("$BREW" --prefix)/bin/python3.12"
  fi
  run "$(t venv)" "$PY" -m venv "$VENV"
fi
run pip "$VPY" -m pip install --upgrade pip --quiet
ok "$("$VPY" --version)"

# ------------------------------------------------------------------ 4. PyTorch + ComfyUI
# PyTorch from PyPI uses the Apple GPU (MPS); it also runs Whisper dictation. ComfyUI is installed for later
# (image generation on macOS is not supported yet: the catalog's image files are made for NVIDIA GPUs).
step "$(t s4)"
if "$VPY" -c 'import importlib.util,sys; sys.exit(importlib.util.find_spec("torch") is None)'; then ok "PyTorch   $(t already)"
else run "$(t torch)" "$VPY" -m pip install torch torchvision torchaudio; fi
if "$VPY" -c 'import sys,torch; sys.exit(not torch.backends.mps.is_available())' 2>/dev/null; then ok "$(t mpsok)"; else warn "$(t nompx)"; fi
getrepo() {  # getrepo "label" url dest branch
  if [ -d "$3" ]; then ok "$1   $(t already)"; return; fi
  if xcode-select -p >/dev/null 2>&1; then run "$1" git clone --depth 1 "$2" "$3"; return; fi
  fetch "$1" "$2/archive/refs/heads/$4.tar.gz" "${TMPDIR:-/tmp}/repo.tgz"
  mkdir -p "$3" && tar xzf "${TMPDIR:-/tmp}/repo.tgz" -C "$3" --strip-components 1 || fail "tar"
}
COMFY="$RUNTIME/ComfyUI"
getrepo ComfyUI https://github.com/comfyanonymous/ComfyUI "$COMFY" master
getrepo IP-Adapter https://github.com/cubiq/ComfyUI_IPAdapter_plus "$COMFY/custom_nodes/ComfyUI_IPAdapter_plus" main
run "$(t deps)" "$VPY" -m pip install -r "$COMFY/requirements.txt" -r "$ROOT/requirements.txt"

# ------------------------------------------------------------------ 5. llama.cpp + Node.js
step "$(t s5)"
LLAMA="$RUNTIME/llama"
if [ ! -x "$LLAMA/llama-server" ]; then
  [ "$ARCH" = arm64 ] && LARCH=arm64 || LARCH=x64
  URL="$(curl -fsSL -H 'User-Agent: sparky' 'https://api.github.com/repos/ggml-org/llama.cpp/releases?per_page=15' | "$VPY" -c '
import json, re, sys
for r in json.load(sys.stdin):
    a = [x for x in r["assets"] if re.match(r"^llama-.*-bin-macos-" + sys.argv[1] + r"\.(tar\.gz|zip)$", x["name"])]
    if a:
        print(a[0]["browser_download_url"]); break' "$LARCH")"
  [ -n "$URL" ] || fail "llama.cpp: no build for macOS $LARCH"
  TAG="$(basename "$(dirname "$URL")")"
  fetch "llama.cpp $TAG" "$URL" "${TMPDIR:-/tmp}/llama.tgz"
  mkdir -p "$LLAMA" && tar xzf "${TMPDIR:-/tmp}/llama.tgz" -C "$LLAMA" --strip-components 1 || fail "tar llama.cpp"
  xattr -dr com.apple.quarantine "$LLAMA" 2>/dev/null
  "$LLAMA/llama-server" --version >>"$LOG" 2>&1 || fail "llama-server"
  ok "llama.cpp $TAG   ·   Metal $LARCH"
else ok "llama.cpp   $(t already)"; fi
# Node.js (portable): runs and tests the JavaScript projects the assistant builds
NODE="$RUNTIME/node"
if [ ! -x "$NODE/bin/node" ]; then
  [ "$ARCH" = arm64 ] && NARCH=arm64 || NARCH=x64
  NV="$(curl -fsSL https://nodejs.org/dist/index.json | "$VPY" -c '
import json, sys
print(next((r["version"] for r in json.load(sys.stdin) if r["lts"] and "osx-" + sys.argv[1] + "-tar" in r["files"]), ""))' "$NARCH")"
  if [ -n "$NV" ]; then
    fetch "Node.js $NV" "https://nodejs.org/dist/$NV/node-$NV-darwin-$NARCH.tar.gz" "${TMPDIR:-/tmp}/node.tgz"
    mkdir -p "$NODE" && tar xzf "${TMPDIR:-/tmp}/node.tgz" -C "$NODE" --strip-components 1 || fail "tar node"
  fi
else ok "Node.js   $(t already)"; fi

# ------------------------------------------------------------------ 6. scan + models
step "$(t s6)"
cd "$ROOT" || fail "$ROOT"
# interface language, saved for the first launch
"$VPY" -c "import json,os,sys;p=os.path.join('data','settings.json');d=json.load(open(p,encoding='utf-8')) if os.path.exists(p) else {};d['ui_lang']=sys.argv[1];json.dump(d,open(p,'w',encoding='utf-8'),indent=1)" "$L"
SCAN="$("$VPY" app/syscheck.py --json "--lang=$L" 2>>"$LOG")" || fail "$(t scan)"
printf '%s' "$SCAN" | "$VPY" -c '
import json, sys
s, gb = json.load(sys.stdin), sys.argv[1]
a, labels, total = s["assess"], dict(zip(("llm", "image", "voice"), sys.argv[5:8])), 0
g, y, z = sys.argv[8:11]
print("    %s✔%s %s %s   ·   %d %s" % (g, z, sys.argv[2], a["tier_label"], round(a["model_mem_gb"]), sys.argv[3]))
for n in s["notes"]:
    print("    %s! %s%s" % (y, n, z))
print("\n    " + sys.argv[4])
for k in ("llm", "image", "voice"):
    m = s["recommended"].get(k)
    if m:
        print("      %-11s%-31s%6.1f %s" % (labels[k], m["label"], m["size_gb"], gb)); total += m["size_gb"]
print("      %-42s%6.1f %s" % ("Total", total, gb))' "$GB" "$(t level)" "$(t usable)" "$(t recommended)" Assistant Images "$([ "$L" = fr ] && echo Voix || echo Voice)" "$C_G" "$C_Y" "$C_0"
if [ -n "$MODELS" ]; then echo; "$VPY" app/models_cli.py --recommended "--lang=$L"; else info "$(t later)"; fi

# ------------------------------------------------------------------ 7. app bundle
# ~/Applications/Sparky.app: a tiny bundle that runs start.sh (found by Spotlight and Launchpad)
step "$(t s7)"
APPB="$HOME/Applications/Sparky.app"
mkdir -p "$APPB/Contents/MacOS" "$APPB/Contents/Resources"
cat >"$APPB/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Sparky</string>
  <key>CFBundleDisplayName</key><string>Sparky</string>
  <key>CFBundleIdentifier</key><string>local.sparky.launcher</string>
  <key>CFBundleExecutable</key><string>Sparky</string>
  <key>CFBundleIconFile</key><string>Sparky</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>$(cat "$ROOT/VERSION" 2>/dev/null)</string>
  <key>LSUIElement</key><true/>
</dict></plist>
EOF
printf '#!/bin/bash\nexec "%s/start.sh"\n' "$ROOT" >"$APPB/Contents/MacOS/Sparky"
chmod +x "$APPB/Contents/MacOS/Sparky" "$ROOT/start.sh" "$ROOT/stop.sh"
ICONSET="${TMPDIR:-/tmp}/Sparky.iconset"; rm -rf "$ICONSET"; mkdir -p "$ICONSET"
for s in 16 32 128 256 512; do
  sips -z $s $s "$ROOT/app/logo.png" --out "$ICONSET/icon_${s}x${s}.png" >/dev/null 2>&1
  sips -z $((s * 2)) $((s * 2)) "$ROOT/app/logo.png" --out "$ICONSET/icon_${s}x${s}@2x.png" >/dev/null 2>&1
done
iconutil -c icns "$ICONSET" -o "$APPB/Contents/Resources/Sparky.icns" 2>>"$LOG"
touch "$APPB"
ok "$(t app)"

# ------------------------------------------------------------------ done
printf '\n   %s╭──────────────────────────────────────────────╮%s\n' "$C_G" "$C_0"
printf '     %s✔  %s%s\n\n     %s\n     %s\n' "$C_G" "$(t ready)" "$C_0" "$(t launchhow)" "$(t launchhow2)"
printf '   %s╰──────────────────────────────────────────────╯%s\n\n' "$C_G" "$C_0"
if [ -z "$NOLAUNCH" ]; then
  info "$(t opening)"
  "$ROOT/start.sh"
  printf '    %s%s%s\n\n' "$C_M" "$(t bye)" "$C_0"
fi
