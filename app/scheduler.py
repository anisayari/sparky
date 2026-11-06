"""Scheduled prompts: "every day at 08:00, summarize the news" runs a prompt with the active assistant
and stores the answer as a new conversation.

Tasks live in data/schedules.json. `when` is one of:
  {"type": "daily", "time": "08:00"}
  {"type": "weekly", "day": 0-6 (0 = Monday), "time": "HH:MM"}
  {"type": "interval", "minutes": N}
  {"type": "once", "at": epoch seconds}
Times are local. A run missed while Sparky was off happens once at the next start.
"""
import contextlib, datetime, os, threading, time, uuid

import config

FILE = os.path.join(config.DATA, "schedules.json")
CHECK_EVERY = 20  # seconds
TYPES = ("daily", "weekly", "interval", "once")

_lock = threading.Lock()   # file
_run_lock = threading.Lock()  # one task at a time
_runner = None  # runner(prompt, title) -> session id, set by start()
_thread = None


# ---------------------------------------------------------------- schedule maths
def _hm(s):
    h, m = (int(x) for x in str(s).split(":"))
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ValueError(f"invalid time {s!r}")
    return h, m


def check_when(when):
    """Normalized copy of `when`; raises ValueError when it is invalid."""
    if not isinstance(when, dict) or when.get("type") not in TYPES:
        raise ValueError("when.type must be one of " + ", ".join(TYPES))
    t = when["type"]
    if t == "daily":
        return {"type": t, "time": "%02d:%02d" % _hm(when.get("time"))}
    if t == "weekly":
        day = int(when.get("day"))
        if not 0 <= day <= 6:
            raise ValueError("when.day must be 0 (Monday) to 6 (Sunday)")
        return {"type": t, "day": day, "time": "%02d:%02d" % _hm(when.get("time"))}
    if t == "interval":
        n = int(when.get("minutes") or 0)
        if n < 1:
            raise ValueError("when.minutes must be at least 1")
        return {"type": t, "minutes": n}
    return {"type": t, "at": float(when.get("at"))}


def next_run(when, now):
    """Next run time (epoch) strictly after `now`, or None (a past "once")."""
    t = when["type"]
    if t == "interval":
        return now + max(1, int(when["minutes"])) * 60
    if t == "once":
        return float(when["at"]) if float(when["at"]) > now else None
    h, m = _hm(when["time"])
    d = datetime.datetime.fromtimestamp(now)
    cand = d.replace(hour=h, minute=m, second=0, microsecond=0)
    if t == "weekly":
        cand += datetime.timedelta(days=(int(when["day"]) - d.weekday()) % 7)
    step = datetime.timedelta(days=7 if t == "weekly" else 1)
    while cand.timestamp() <= now:
        cand += step
    return cand.timestamp()


# ---------------------------------------------------------------- storage
def _load():
    return config.read_json(FILE, []) or []


def _save(tasks):
    config.write_json(FILE, tasks)


def list_():
    with _lock:
        return _load()


def get(tid):
    return next((t for t in list_() if t["id"] == tid), None)


def save_task(task, now=None):
    """Creates (no id) or updates a task; returns it with its next_run."""
    now = time.time() if now is None else now
    prompt = (task.get("prompt") or "").strip()
    if not prompt:
        raise ValueError("prompt is required")
    when = check_when(task.get("when"))
    with _lock:
        tasks = _load()
        old = next((t for t in tasks if t["id"] == task.get("id")), None) if task.get("id") else None
        t = old or {"id": uuid.uuid4().hex[:12], "last_run": None, "last_session": None, "last_error": None,
                    "unread": False, "created": now}
        if (t.get("run") or {}).get("phase") in ("search", "summarize", "review"):
            raise ValueError("Terminez ou annulez la séquence en cours avant de la modifier.")
        web = task.get("web", t.get("web", bool(old)))
        if type(web) is not bool:
            raise ValueError("web must be true or false")
        collection = task.get("collection", t.get("collection"))
        if collection is not None and type(collection) is not int:
            raise ValueError("collection must be an identifier")
        documents = task.get("compare_documents", t.get("compare_documents", []))
        if (not isinstance(documents, list) or (documents and
                (collection is None or len(documents) != 2 or any(type(i) is not int for i in documents)
                 or documents[0] == documents[1]))):
            raise ValueError("Sélectionne deux versions distinctes dans la même collection.")
        if documents:
            import document_diff
            document_diff.validate(collection, documents)
        model = task.get("model", t.get("model"))
        if model is not None and not isinstance(model, str):
            raise ValueError("invalid model")
        t.update(title=(task.get("title") or prompt[:60]).strip(), prompt=prompt, when=when,
                 enabled=bool(task.get("enabled", True)), web=web, model=model or None, collection=collection,
                 compare_documents=documents)
        t["next_run"] = next_run(when, now) if t["enabled"] else None
        if not old:
            tasks.append(t)
        _save(tasks)
        return dict(t)


def delete(tid):
    with _lock:
        tasks = _load()
        left = [t for t in tasks if t["id"] != tid]
        _save(left)
        return len(left) != len(tasks)


@contextlib.contextmanager
def workflow_sources(task_id, run_id):
    """Only the current synthesis run can transmit passages from its assigned collection."""
    with _lock:
        task = next((row for row in _load() if row["id"] == task_id), {})
        run = task.get("run") or {}
        yield [task["collection"]] if run.get("id") == run_id and run.get("phase") == "summarize" else []


def unread():
    """Ids of tasks with a result the user has not opened yet (UI badge)."""
    return [t["id"] for t in list_() if t.get("unread")]


def mark_read(tid):
    _update(tid, unread=False)


def _update(tid, *, expected_run=None, **fields):
    with _lock:
        tasks = _load()
        for t in tasks:
            if t["id"] == tid:
                r = t.get("run") or {}
                if expected_run is not None and (r.get("id"), r.get("phase")) != expected_run:
                    return None
                t.update(fields)
                _save(tasks)
                return dict(t)
    return None


# ---------------------------------------------------------------- running
def review_result(tid, run_id, decision):
    if decision not in ("approved", "rejected"):
        raise ValueError("invalid review decision")
    task = get(tid)
    run = (task or {}).get("run") or {}
    if run.get("phase") != "review" or run["id"] != run_id:
        return None
    if decision == "approved":
        import workflows
        if not run.get("digest") or workflows.result_digest(run["session"], run_id) != run["digest"]:
            return None
    return _update(tid, expected_run=(run_id, "review"), run=run | {"phase": decision, "reviewed": time.time()})


def remove_result(sid):
    with _lock:
        tasks = _load()
        changed = False
        for task in tasks:
            if task.get("last_session") == sid:
                task.update(last_session=None, unread=False)
                if (task.get("run") or {}).get("session") == sid:
                    task["run"]["phase"] = "removed"
                changed = True
        if changed:
            _save(tasks)


def _resuming(t):
    return (t.get("run") or {}).get("phase") in ("search", "summarize")


def cancel_workflow(tid, run_id):
    task = get(tid)
    if not task or not _resuming(task) or task["run"]["id"] != run_id:
        return None
    run = task["run"]
    once = task["when"]["type"] == "once"
    enabled = bool(task["enabled"] and not once)
    return _update(tid, expected_run=(run_id, run["phase"]),
                   run={"id": run_id, "session": run["session"], "phase": "cancelled"},
                   enabled=enabled, next_run=next_run(task["when"], time.time()) if enabled else None)


def _run(task, runner, due_at=None):
    """Runs one task now and records the outcome (the task may have been edited or deleted meanwhile).
    due_at: only run if the task is still due at that time (another run may have just rescheduled it)."""
    with _run_lock:
        task = get(task["id"])
        if not task or (task.get("run") or {}).get("phase") == "review":
            return None
        if due_at is not None and not _resuming(task) and (
                not task.get("enabled") or task.get("next_run") is None or task["next_run"] > due_at):
            return None
        started, sid, err = time.time(), None, None
        run = None
        try:
            if task.get("collection") is not None:
                import workflows
                run = task.get("run") if _resuming(task) else None
                if not run:
                    run = workflows.new_run()
                    if not _update(task["id"], run=run):
                        return None
                def checkpoint(phase, **state):
                    nonlocal run
                    new = run | state | {"phase": phase}
                    if not _update(task["id"], expected_run=(run["id"], run["phase"]), run=new):
                        raise ValueError("Séquence annulée.")
                    run = new
                sid = workflows.summarize(task, run, checkpoint)
                digest = workflows.result_digest(sid, run["id"])
                if not digest:
                    sid = None
                    raise ValueError("La synthèse enregistrée est introuvable.")
            elif runner is _runner:
                sid = runner(task["prompt"], task["title"], task)
            else:
                sid = runner(task["prompt"], task["title"])
        except Exception as e:
            err = str(e) or repr(e)
            print("scheduler:", task["title"], ":", repr(e))
        cur = get(task["id"])
        if not cur:
            return None
        done = time.time()
        once = cur["when"]["type"] == "once"
        fields = {"last_run": started, "last_error": err,
                  "next_run": None if once or not cur["enabled"] else next_run(cur["when"], done)}
        if run:
            fields["run"] = {k: run[k] for k in ("id", "session")} | {"phase": "failed" if err else "review"}
            if sid:
                fields["run"]["digest"] = digest
        if once:
            fields["enabled"] = False
        if sid:
            fields.update(last_session=sid, unread=True)
        return _update(task["id"], expected_run=(run["id"], run["phase"]) if run else None, **fields)


def tick(now=None, runner=None):
    """Runs every due task, one after the other. Returns the ids that ran."""
    runner = runner or _runner
    if not runner:
        return []
    now = time.time() if now is None else now
    due = [t for t in list_() if _resuming(t) or (t.get("enabled") and t.get("next_run") is not None and t["next_run"] <= now)]
    return [t["id"] for t in sorted(due, key=lambda t: t["next_run"] or 0) if _run(t, runner, now)]


def run_now(tid, wait=False, runner=None):
    """Runs a task right away (in the background unless wait). False when it does not exist."""
    task, runner = get(tid), runner or _runner
    if not task or not runner or (task.get("run") or {}).get("phase") == "review":
        return False
    if wait:
        _run(task, runner)
    else:
        threading.Thread(target=_run, args=(task, runner), daemon=True).start()
    return True


def start(runner):
    """Registers the runner and starts the background checker (once)."""
    global _runner, _thread
    _runner = runner
    if _thread and _thread.is_alive():
        return

    def loop():
        while True:
            time.sleep(CHECK_EVERY)
            try:
                tick()
            except Exception as e:
                print("scheduler:", repr(e))
    _thread = threading.Thread(target=loop, daemon=True, name="scheduler")
    _thread.start()
