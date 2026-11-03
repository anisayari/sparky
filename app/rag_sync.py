"""Synchronize explicitly selected local folders; links never extend a collection's scope."""
import hashlib
import os
from contextlib import ExitStack, contextmanager
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


if os.name == "nt":
    import ctypes
    from ctypes import wintypes
    import msvcrt

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    _create_file = kernel.CreateFileW
    _create_file.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                             wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    _create_file.restype = wintypes.HANDLE
    _close = kernel.CloseHandle
    _close.argtypes = (wintypes.HANDLE,)
    _final_name = kernel.GetFinalPathNameByHandleW
    _final_name.argtypes = (wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD)
    _final_name.restype = wintypes.DWORD
    _attributes = kernel.GetFileInformationByHandleEx
    _attributes.argtypes = (wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD)
    _attributes.restype = wintypes.BOOL

    def _verify_name(handle, expected):
        buf = ctypes.create_unicode_buffer(32768)
        n = _final_name(handle, buf, len(buf), 0)
        if not 0 < n < len(buf) or Path(buf.value.removeprefix("\\\\?\\")) != expected:
            raise ValueError("Chemin modifié ou sorti du dossier choisi.")

    def _win_handle(path, *, directory):
        # OPEN_REPARSE_POINT must be present on the first open, even for the leaf:
        # querying the target and rejecting it afterwards could contact a network share.
        flags = 0x00200000 | 0x02000000  # OPEN_REPARSE_POINT | BACKUP_SEMANTICS
        access = 0x80 if directory else 0x80000000  # FILE_READ_ATTRIBUTES / GENERIC_READ
        share = 3 if directory else 1  # no rename/delete, or writes during a file read
        handle = _create_file(str(path), access, share, None, 3, flags, None)
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            attrs = (wintypes.DWORD * 2)()
            if not _attributes(handle, 9, ctypes.byref(attrs), ctypes.sizeof(attrs)):  # FileAttributeTagInfo
                raise ctypes.WinError(ctypes.get_last_error())
            if attrs[0] & 0x400 or bool(attrs[0] & 0x10) != directory:
                raise ValueError("Lien, jonction ou type de fichier non autorisé.")
            _verify_name(handle, path)
            return handle
        except BaseException:
            _close(handle)
            raise


@contextmanager
def _directory(path):
    """Open each component from the volume root. Never turn a substituted link into an authorized path."""
    with ExitStack() as handles:
        if os.name == "nt":
            for part in (*reversed(path.parents), path):
                # Open the reparse point itself. Without FILE_SHARE_DELETE no parent can be
                # renamed/replaced while its children are opened or the directory is listed.
                handles.callback(_close, _win_handle(part, directory=True))
            yield path
        else:
            flags = os.O_DIRECTORY | os.O_NOFOLLOW | os.O_RDONLY
            fd = os.open(path.anchor, flags)
            handles.callback(os.close, fd)
            for part in path.parts[1:]:
                fd = os.open(part, flags, dir_fd=fd)
                handles.callback(os.close, fd)
            yield fd


def _folder(value):
    root = Path(value)
    if not root.is_absolute() or root == Path(root.anchor) or ".." in root.parts or str(root).startswith(("\\\\", "//")):
        raise ValueError("Choisissez un dossier local précis.")
    try:
        with _directory(root):
            if _private(root):
                raise ValueError("Le dossier interne de Sparky ne peut pas être synchronisé.")
    except OSError as exc:
        raise ValueError("Dossier indisponible ou inaccessible.") from exc
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


def _read_file(root, rel, *, stamp_only=False):
    if rel.anchor or not rel.parts or ".." in rel.parts:
        raise ValueError("Chemin relatif non autorisé.")
    path = root / rel
    with _directory(path.parent) as directory:
        if os.name == "nt":
            handle = _win_handle(path, directory=False)
            try:
                fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
            except BaseException:
                _close(handle)
                raise
            with os.fdopen(fd, "rb") as f:  # ownership transferred to the CRT
                return _bounded_read(f, stamp_only)
        with os.fdopen(os.open(rel.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory), "rb") as f:
            return _bounded_read(f, stamp_only)


def _stamp(info):
    # Win32 stat(path) reports creation time where fstat(handle) can report change time.
    changed = info.st_ctime_ns if os.name != "nt" else 0
    return f"{info.st_dev}:{info.st_ino}:{info.st_size}:{info.st_mtime_ns}:{changed}"


def _bounded_read(f, stamp_only=False):
    info = os.fstat(f.fileno())
    if not stat.S_ISREG(info.st_mode) or _linked(info) or info.st_nlink > 1 or info.st_size > attach.MAX_BYTES:
        raise ValueError("fichier lié, non ordinaire ou trop volumineux")
    stamp = _stamp(info)
    if stamp_only:
        # On Windows a writer can restore the original mtime after a same-size edit.
        # Hash unchanged candidates without retaining their contents in memory.
        digest = hashlib.sha256()
        count = 0
        while chunk := f.read(min(1 << 20, attach.MAX_BYTES + 1 - count)):
            count += len(chunk)
            digest.update(chunk)
        data = None
        stamp += ":" + digest.hexdigest()
    else:
        data = f.read(attach.MAX_BYTES + 1)
        count = len(data)
        if os.name == "nt":
            stamp += ":" + hashlib.sha256(data).hexdigest()
    if count != info.st_size or _stamp(os.fstat(f.fileno())) != _stamp(info):
        raise ValueError("fichier modifié pendant la lecture")
    return data, stamp


def _scan_files(root, note):
    pending = [root]
    found = {}
    while pending:
        base = pending.pop()
        try:
            # On Windows the directory chain stays locked while enumerating. On POSIX
            # scandir and entry.stat use the opened descriptor, independent of renames.
            with _directory(base) as directory, os.scandir(directory) as entries:
                for entry in entries:
                    if entry.name.startswith("."):
                        continue
                    p = base / entry.name
                    try:
                        # Windows exposes initially enumerated attributes here. The verified
                        # leaf handle below supplies the definitive link count and file ID.
                        info = entry.stat(follow_symlinks=False)
                        if _linked(info):
                            continue
                        if stat.S_ISDIR(info.st_mode):
                            if not _private(p):
                                pending.append(p)
                        elif stat.S_ISREG(info.st_mode) and info.st_nlink <= 1 and p.suffix.lower() in EXTENSIONS:
                            if len(found) >= 5000:
                                raise OverflowError("limite de 5 000 documents par dossier")
                            relative = p.relative_to(root)
                            stamp = _read_file(root, relative, stamp_only=True)[1] if os.name == "nt" else _stamp(info)
                            found[relative.as_posix()] = stamp
                    except (OSError, ValueError) as exc:
                        note(exc)
        except (OSError, ValueError) as exc:
            note(exc)
    return found


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
            found = _scan_files(root, note)
        except (OSError, OverflowError, ValueError) as e:
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
