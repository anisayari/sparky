"""Own the engines we launch. Windows stops use kernel objects, never port owners or PIDs."""
import ctypes
import hashlib
import os
import subprocess
import sys
import threading
import uuid
from contextlib import contextmanager


if os.name == "nt":
    from ctypes import wintypes as wt

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)

    def _api(name, result, *args):
        fn = getattr(kernel, name)
        fn.restype, fn.argtypes = result, args
        return fn

    create_mutex = _api("CreateMutexW", wt.HANDLE, wt.LPVOID, wt.BOOL, wt.LPCWSTR)
    release_mutex = _api("ReleaseMutex", wt.BOOL, wt.HANDLE)
    create_event = _api("CreateEventW", wt.HANDLE, wt.LPVOID, wt.BOOL, wt.BOOL, wt.LPCWSTR)
    wait_handle = _api("WaitForSingleObject", wt.DWORD, wt.HANDLE, wt.DWORD)
    close_handle = _api("CloseHandle", wt.BOOL, wt.HANDLE)
    create_job = _api("CreateJobObjectW", wt.HANDLE, wt.LPVOID, wt.LPCWSTR)
    set_limits = _api("SetInformationJobObject", wt.BOOL, wt.HANDLE, ctypes.c_int, wt.LPVOID, wt.DWORD)
    open_job = _api("OpenJobObjectW", wt.HANDLE, wt.DWORD, wt.BOOL, wt.LPCWSTR)
    join_job = _api("AssignProcessToJobObject", wt.BOOL, wt.HANDLE, wt.HANDLE)
    current_process = _api("GetCurrentProcess", wt.HANDLE)

    class _BasicLimits(ctypes.Structure):
        _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64), ("flags", wt.DWORD),
                    ("min_ws", ctypes.c_size_t), ("max_ws", ctypes.c_size_t), ("max_processes", wt.DWORD),
                    ("affinity", ctypes.c_size_t), ("priority", wt.DWORD), ("scheduling", wt.DWORD)]

    class _ExtendedLimits(ctypes.Structure):
        _fields_ = [("basic", _BasicLimits), ("io", ctypes.c_uint64 * 6), ("memory", ctypes.c_size_t * 4)]

    def _check(value):
        if not value:
            raise ctypes.WinError(ctypes.get_last_error())
        return value


@contextmanager
def instance(root):
    """A stop event and completion mutex, scoped to this installation and logon session."""
    if os.name != "nt":
        yield lambda: False
        return
    digest = hashlib.sha256(os.path.normcase(os.path.abspath(root)).encode("utf-8")).hexdigest()
    name = "Local\\Sparky-" + digest
    mutex = _check(create_mutex(None, True, name + "-running"))
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        close_handle(mutex)
        raise RuntimeError("Une instance de Sparky est déjà active dans cette installation.")
    try:
        event = _check(create_event(None, True, False, name + "-stop"))
        try:
            yield lambda: wait_handle(event, 0) == 0
        finally:
            close_handle(event)
    finally:
        release_mutex(mutex)
        close_handle(mutex)


def _engine_job():
    name = "Local\\Sparky-engine-" + uuid.uuid4().hex
    job = _check(create_job(None, name))
    try:
        if ctypes.get_last_error() == 183:
            raise RuntimeError("Job Windows déjà existant.")
        limits = _ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        _check(set_limits(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)))
    except BaseException:
        close_handle(job)
        raise
    return name, job


def _run_in_job(name, cmd):
    # The launcher joins before it can start an engine or one of its descendants.
    # If the server died first, opening the job fails, or closing our handle kills us.
    job = _check(open_job(0x0001, False, name))  # JOB_OBJECT_ASSIGN_PROCESS
    try:
        _check(join_job(job, current_process()))
    finally:
        close_handle(job)
    return subprocess.call(cmd, stdin=subprocess.DEVNULL, stdout=sys.stdout, stderr=subprocess.STDOUT,
                           creationflags=subprocess.CREATE_NO_WINDOW)


class Engine:
    def __init__(self, cmd, log, cwd):
        self.job = None
        if os.name == "nt":
            name, self.job = _engine_job()
            cmd = [sys.executable, "-I", "-S", os.path.abspath(__file__), "--in-job", name, *cmd]
        try:
            with open(log, "w") as out:
                self.process = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except BaseException:
            if self.job is not None:
                close_handle(self.job)
            raise

    def running(self):
        return self.process.poll() is None

    def stop(self):
        if self.job is not None:
            _check(close_handle(self.job))
            self.job = None
        elif os.name != "nt" and self.running():
            self.process.kill()
        self.process.wait(timeout=10)


class Engines:
    def __init__(self):
        self.lock = threading.RLock()
        self.children = {}
        self.closed = False

    def spawn(self, role, cmd, log, cwd=None):
        with self.lock:
            if self.closed:
                raise RuntimeError("Sparky est en cours d'arrêt.")
            if role in self.children:
                if self.children[role].running():
                    return False
                self.stop(role)
            self.children[role] = Engine(cmd, log, cwd)
            return True

    def stop(self, role):
        with self.lock:
            if role in self.children:
                self.children[role].stop()
                del self.children[role]

    def close(self):
        with self.lock:
            self.closed = True
            try:
                self.stop("llm")
            finally:
                self.stop("comfy")


if __name__ == "__main__" and os.name == "nt" and sys.argv[1:2] == ["--in-job"]:
    sys.exit(_run_in_job(sys.argv[2], sys.argv[3:]))
