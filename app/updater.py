"""Sparky version and updates.

The version lives in the VERSION file at the project root. Once an hour (and at startup) Sparky reads
the VERSION file of the GitHub repository; when it is newer, the UI offers the update:
  - git install, no local change: "git pull --ff-only" then Sparky restarts by itself;
  - local changes, or an install from the ZIP: the UI explains why and gives the command to run.
"""
import os, re, shutil, subprocess, sys, threading, time, urllib.request

import config

REPO = "anisayari/sparky"
REMOTE_VERSION = os.environ.get("SPARKY_VERSION_URL") or f"https://raw.githubusercontent.com/{REPO}/main/VERSION"
INSTALL_URL = f"https://raw.githubusercontent.com/{REPO}/main/install.ps1"
NOWIN = config.NOWIN
CHECK_EVERY = 3600

_lock = threading.Lock()
state = {"latest": None, "checked": 0.0, "error": "", "updating": False, "log": ""}


def current():
    try:
        return open(os.path.join(config.ROOT, "VERSION"), encoding="utf-8").read().strip()
    except OSError:
        return "0.0.0"


def _num(v):
    return tuple(int(x) for x in re.findall(r"\d+", v or "")[:3]) or (0,)


def _git(*args, timeout=60):
    r = subprocess.run(["git", "-C", config.ROOT, *args], capture_output=True, text=True, timeout=timeout,
                       creationflags=NOWIN, encoding="utf-8", errors="replace")
    return r.returncode, r.stdout + r.stderr  # not stripped: "git status --porcelain" lines start with a space


def is_git():
    return bool(shutil.which("git")) and os.path.isdir(os.path.join(config.ROOT, ".git"))


def local_changes():
    """Tracked files modified by hand (untracked files do not block a fast-forward pull)."""
    if not is_git():
        return []
    code, out = _git("status", "--porcelain", "--untracked-files=no")
    return [l[3:].strip() for l in out.splitlines() if len(l) > 3] if code == 0 else []


def check(force=False):
    if not force and time.time() - state["checked"] < CHECK_EVERY:
        return
    state["checked"] = time.time()
    try:
        req = urllib.request.Request(REMOTE_VERSION + f"?t={int(time.time())}", headers={"Cache-Control": "no-cache"})
        with urllib.request.urlopen(req, timeout=10) as r:
            state.update(latest=r.read().decode().strip(), error="")
    except Exception as e:  # offline: Sparky works without Internet, so this is not an error for the user
        state["error"] = str(e)


def loop():
    while True:
        try:
            check()
        except Exception:
            pass
        time.sleep(300)


def status():
    cur, latest = current(), state["latest"]
    pending = config.read_json(_pending_file(), {}) or {}
    avail = bool(pending) or (bool(latest) and _num(latest) > _num(cur))
    git, changes = is_git(), local_changes() if avail else []
    root = config.ROOT.replace("'", "''")
    return {"version": cur, "latest": latest or pending.get("target"), "available": avail, "git": git, "changes": changes,
            "auto": avail and git and not changes, "updating": state["updating"], "log": state["log"],
            "command": f"& ([scriptblock]::Create((irm '{INSTALL_URL}'))) -Dir '{root}'", "root": config.ROOT}


def _pending_file():
    return os.path.join(config.ROOT, "data", "update-pending.json")


def _finish_dependencies():
    req = os.path.join(config.ROOT, "requirements.txt")
    if os.path.isfile(req):
        r = subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-r", req], creationflags=NOWIN, timeout=900,
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        if r.returncode:
            return False, "installation des dépendances impossible : " + (r.stderr or r.stdout)[-400:]
    os.remove(_pending_file())
    return True, ""


def repair_on_start():
    """Run before server imports, even when the new code requires a package that failed to install."""
    pending = config.read_json(_pending_file(), {}) or {}
    if not pending or (not pending.get("dependencies") and current() == pending.get("base")):
        return True, ""  # a failed pull hasn't changed the code; the UI can still retry it
    if not is_git() or local_changes():
        return False, "Sources modifiées : vérifiez les changements avant de réparer les dépendances."
    return _finish_dependencies()


def _restart():
    """Starts a new Sparky once this process has released its port, then exits."""
    if os.name != "nt":
        return
    q = lambda p: p.replace("'", "''")
    script = (f"Start-Sleep -Seconds 2; Start-Process '{q(sys.executable)}' -ArgumentList 'app\\server.py','--port','{config.APP_PORT}' "
              f"-WorkingDirectory '{q(config.ROOT)}' -WindowStyle Hidden "
              f"-RedirectStandardOutput '{q(os.path.join(config.DATA, 'server.log'))}' "
              f"-RedirectStandardError '{q(os.path.join(config.DATA, 'server.err.log'))}'")
    cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script]
    # no DETACHED_PROCESS: powershell dies without a console. Breakaway: survive a job that kills its children.
    flags = NOWIN | subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        subprocess.Popen(cmd, creationflags=flags | 0x01000000, close_fds=True)  # CREATE_BREAKAWAY_FROM_JOB
    except OSError:
        subprocess.Popen(cmd, creationflags=flags, close_fds=True)
    time.sleep(0.5)
    os._exit(0)


def update():
    """git pull --ff-only, then restart. Returns (ok, message)."""
    with _lock:  # one update at a time (two tabs, two clicks)
        if state["updating"]:
            return False, "mise à jour déjà en cours"
        s = status()
        if not s["available"]:
            return False, "déjà à jour"
        if not s["git"]:
            return False, "installation sans git : relance la commande d'installation"
        if s["changes"]:
            return False, "modifications locales : " + ", ".join(s["changes"][:8])
        state.update(updating=True, log="")
    try:
        marker = _pending_file()
        pending = config.read_json(marker, {}) or {"base": current(), "target": s["latest"]}
        out = ""
        if not pending.get("dependencies") and current() == pending["base"]:
            # Record intent before pull, since it changes VERSION before dependencies are installed.
            config.write_json(marker, pending)
            code, out = _git("pull", "--ff-only", timeout=180)
            out = out.strip()
            state["log"] = out[-1500:]
            if code != 0:
                return False, "git pull a échoué : " + out[-400:]
        pending["dependencies"] = True
        config.write_json(marker, pending)
        ok, error = _finish_dependencies()
        if not ok:
            return False, error
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)
    finally:
        state["updating"] = False
    threading.Thread(target=_restart, daemon=True).start()
    return True, out[-400:]


def start():
    threading.Thread(target=loop, daemon=True).start()


if __name__ == "__main__" and sys.argv[1:] == ["--repair"]:
    try:
        ok, message = repair_on_start()
    except (OSError, subprocess.TimeoutExpired) as e:
        ok, message = False, str(e)
    if not ok:
        sys.exit(message)
