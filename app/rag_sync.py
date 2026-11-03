"""Synchronize explicitly selected local folders; links never extend a collection's scope."""
import os
from pathlib import Path
import stat
import threading
import time

import attach
import config
import rag

_wakeup = threading.Event()
_scan_lock = threading.Lock()
EXTENSIONS = attach.TEXT_EXT | set(attach.OFFICE) | {".pdf"}


def _linked(info):
    return stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400


def _folder(value):
    root = Path(value)
    if not root.is_absolute() or root == Path(root.anchor) or str(root).startswith(("\\\\", "//")):
        raise ValueError("Choisissez un dossier local précis.")
    try:
        linked = any(_linked(p.lstat()) for p in (root, *root.parents))
    except OSError as exc:
        raise ValueError("Dossier indisponible ou inaccessible.") from exc
    if linked or not root.is_dir():
        raise ValueError("Les liens et jonctions ne peuvent pas servir de dossier source.")
    root = root.resolve(strict=True)
    if _private(root):
        raise ValueError("Le dossier interne de Sparky ne peut pas être synchronisé.")
    return root


def _private(path):
    return any(path.is_relative_to(Path(folder).resolve()) for folder in
               (config.DATA, config.RUNTIME, config.COMFY_DIR, config.LLM_DIR, config.ROOT))


def set_folder(cid, value):
    folder = str(_folder(value)) if value else ""
    with rag._lock, rag.db() as conn:
        row = conn.execute("SELECT folder FROM collections WHERE id=?", (cid,)).fetchone()
        if not row:
            raise ValueError("collection inconnue")
        if row["folder"] != folder:
            conn.execute("DELETE FROM documents WHERE collection_id=? AND source='folder'", (cid,))
            conn.execute("UPDATE collections SET folder=?, last_sync=NULL, sync_status='' WHERE id=?", (folder, cid))
    _wakeup.set()


def _read_file(root, rel):
    path = root / rel
    if os.name == "nt":
        import ctypes
        import msvcrt
        from ctypes import wintypes
        f = path.open("rb")
        try:
            final = ctypes.windll.kernel32.GetFinalPathNameByHandleW
            final.argtypes = (wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD)
            final.restype = wintypes.DWORD
            buf = ctypes.create_unicode_buffer(32768)
            n = final(msvcrt.get_osfhandle(f.fileno()), buf, len(buf), 0)
            if not (0 < n < len(buf)) or Path(buf.value.removeprefix("\\\\?\\")) != path:
                raise ValueError("fichier sorti du dossier choisi")
            return _bounded_read(f)
        finally:
            f.close()
    fd = os.open(root, os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in rel.parts[:-1]:
            child = os.open(part, os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        with os.fdopen(os.open(rel.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd), "rb") as f:
            return _bounded_read(f)
    finally:
        os.close(fd)


def _stamp(info):
    # Win32 stat(path) reports creation time where fstat(handle) can report change time.
    changed = info.st_ctime_ns if os.name != "nt" else 0
    return f"{info.st_dev}:{info.st_ino}:{info.st_size}:{info.st_mtime_ns}:{changed}"


def _bounded_read(f):
    info = os.fstat(f.fileno())
    if not stat.S_ISREG(info.st_mode) or _linked(info) or info.st_nlink > 1 or info.st_size > attach.MAX_BYTES:
        raise ValueError("fichier lié, non ordinaire ou trop volumineux")
    data = f.read(attach.MAX_BYTES + 1)
    if len(data) > attach.MAX_BYTES or _stamp(os.fstat(f.fileno())) != _stamp(info):
        raise ValueError("fichier modifié pendant la lecture")
    return data, _stamp(info)


def scan(cid, folder):
    with _scan_lock:
        old = {r["name"]: r for r in rag._read("SELECT id,name,sync_stamp,state FROM documents "
                                               "WHERE collection_id=? AND source='folder'", (cid,))}
        added = changed = removed = errors = 0
        found = {}
        notes = []
        def note(exc):
            if len(notes) < 3:
                notes.append(str(exc))
        try:
            root = _folder(folder)
            for directory, folders, files in os.walk(root, onerror=note):
                base = Path(directory)
                allowed = []
                for name in folders:
                    candidate = base / name
                    try:
                        if not name.startswith(".") and not _private(candidate) and not _linked(candidate.lstat()):
                            allowed.append(name)
                    except OSError as e:
                        note(e)
                folders[:] = allowed
                for name in files:
                    p = base / name
                    if name.startswith(".") or p.suffix.lower() not in EXTENSIONS:
                        continue
                    try:
                        info = p.lstat()
                        if stat.S_ISREG(info.st_mode) and not _linked(info) and info.st_nlink <= 1:
                            if len(found) >= 5000:
                                raise ValueError("limite de 5 000 documents par dossier")
                            found[p.relative_to(root).as_posix()] = _stamp(info)
                    except OSError as e:
                        note(e)
        except (OSError, ValueError) as e:
            note(e)
            found.clear()
        # Revoke retired or replaced files before new extraction, including inaccessible subtrees.
        for name, row in old.items():
            if name not in found or row["sync_stamp"] != found[name]:
                rag.delete_document(row["id"])
                if name not in found:
                    removed += 1
        for name, stamp in found.items():
            if name in old and old[name]["sync_stamp"] == stamp:
                continue
            try:
                data, read_stamp = _read_file(root, Path(name))
                if read_stamp != stamp:
                    raise ValueError("fichier en cours de modification")
                rag.add_document(cid, name, data, source="folder", folder=folder, sync_stamp=stamp)
                if name in old:
                    changed += 1
                else:
                    added += 1
            except (OSError, ValueError) as e:
                errors += 1
                note(f"{name}: {e}")
        status = f"+{added} / modifiés {changed} / retirés {removed} / erreurs {max(errors, len(notes))}"
        if notes:
            status += " · " + " · ".join(notes)
        rag._write("UPDATE collections SET last_sync=?,sync_status=? WHERE id=? AND folder=?",
                   (time.time(), status, cid, folder))


def start(stop_requested):
    def run():
        next_scan = 0
        while not stop_requested():
            try:
                rag.resume()
                if time.monotonic() >= next_scan or _wakeup.is_set():
                    _wakeup.clear()
                    for row in rag._read("SELECT id,folder FROM collections WHERE folder != ''"):
                        if stop_requested():
                            return
                        scan(row["id"], row["folder"])
                    next_scan = time.monotonic() + 60
                count = rag.reindex()
            except Exception as exc:
                print("knowledge maintenance:", exc)
                count = 0
            _wakeup.wait(0.5 if count else 5)
    threading.Thread(target=run, daemon=True, name="knowledge-maintenance").start()
