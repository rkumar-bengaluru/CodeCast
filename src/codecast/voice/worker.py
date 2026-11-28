"""CodeCast's side of a long-lived voice worker process (spec 5.4, 5.5): spawn, talk, time out, stop, never orphan."""
from __future__ import annotations

import atexit
import json
import logging
import os
import queue
import subprocess
import sys
import threading
import time
import weakref
from collections import deque
from collections.abc import Mapping
from pathlib import Path

from codecast.errors import CodecastError

PROTOCOL = 1
QUIT_WAIT_S = 10.0  # close(): how long a worker gets to finish the sentence in hand after `quit`
LOCK_POLL_S = 1.0
STDERR_KEEP = 40

_ENV_NAMES = {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "HOME", "USERPROFILE", "LOCALAPPDATA", "APPDATA"}
_ENV_PREFIXES = ("HF_", "HUGGINGFACE_", "TRANSFORMERS_", "CUDA_", "PYTORCH_", "TORCH_", "NVIDIA_")
_ENV_FIXED = {"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}

_LIVE: weakref.WeakSet[Worker] = weakref.WeakSet()


def worker_env(environ: Mapping[str, str]) -> dict[str, str]:
    """The allowlist: what a worker may see of CodeCast's environment (API keys and the rest of .env never)."""
    env = {k: v for k, v in environ.items() if k.upper() in _ENV_NAMES or k.upper().startswith(_ENV_PREFIXES)}
    env.update(_ENV_FIXED)
    return env


# --- platform helpers -------------------------------------------------------------------------------------------------

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _ntdll = ctypes.WinDLL("ntdll")
    _JobObjectExtendedLimitInformation = 9
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    _CREATE_SUSPENDED = 0x4
    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _STILL_ACTIVE = 259
    _ERROR_ACCESS_DENIED = 5

    class _BasicLimits(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class _IoCounters(ctypes.Structure):
        _fields_ = [(n, ctypes.c_uint64) for n in ("ReadOps", "WriteOps", "OtherOps", "ReadBytes", "WriteBytes",
                                                   "OtherBytes")]

    class _ExtendedLimits(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", _BasicLimits), ("IoInfo", _IoCounters),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    _k32.CreateJobObjectW.restype = wintypes.HANDLE
    _k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    _k32.SetInformationJobObject.restype = wintypes.BOOL
    _k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    _k32.AssignProcessToJobObject.restype = wintypes.BOOL
    _k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _k32.TerminateJobObject.restype = wintypes.BOOL
    _k32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _k32.CloseHandle.restype = wintypes.BOOL
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _k32.GetExitCodeProcess.restype = wintypes.BOOL
    _k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    _ntdll.NtResumeProcess.restype = ctypes.c_long
    _ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]

    def _make_job(proc_handle: int) -> int | None:
        """A Job Object that kills everything in it when its last handle closes; None if Windows refused."""
        job = _k32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = _ExtendedLimits()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not (_k32.SetInformationJobObject(job, _JobObjectExtendedLimitInformation, ctypes.byref(info),
                                             ctypes.sizeof(info))
                and _k32.AssignProcessToJobObject(job, proc_handle)):
            _k32.CloseHandle(job)
            return None
        return job

    def _resume(proc_handle: int) -> bool:
        return _ntdll.NtResumeProcess(proc_handle) >= 0

    def _close_job(job: int) -> None:
        _k32.TerminateJobObject(job, 1)
        _k32.CloseHandle(job)

    def _pid_alive(pid: int) -> bool:
        h = _k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return ctypes.get_last_error() == _ERROR_ACCESS_DENIED  # exists, but not ours to query
        try:
            code = wintypes.DWORD()
            return bool(_k32.GetExitCodeProcess(h, ctypes.byref(code))) and code.value == _STILL_ACTIVE
        finally:
            _k32.CloseHandle(h)
else:
    import signal

    def _pid_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True


if sys.platform == "win32":
    import msvcrt

    def _try_lock(fd: int) -> bool:
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)  # byte 0; Windows byte locks are mandatory, so the pid starts at 1
        except OSError:
            return False
        return True

    def _unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _try_lock(fd: int) -> bool:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        return True

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


_HELD: set[str] = set()  # GPU lock files held by this process
_HELD_MUTEX = threading.Lock()


def _lock_holder(path: Path) -> int | None:
    """The pid the holder wrote after byte 0 (best effort, only for the wait message)."""
    try:
        with open(path, "rb") as f:
            f.seek(1)
            return int(f.read(32).strip() or b"x")
    except (OSError, ValueError):
        return None


# --- the client ---------------------------------------------------------------------------------------------------------

class Worker:
    """One worker process: `start` once, `request` one at a time, `close` (always)."""

    def __init__(self, argv: list[str], *, start_timeout_s: float, sentence_timeout_s: float,
                 lock_device: str | None, lock_dir: Path, log: logging.Logger):
        self.argv = [str(a) for a in argv]
        self.start_timeout_s = start_timeout_s
        self.sentence_timeout_s = sentence_timeout_s
        self.lock_device = lock_device
        self.lock_dir = Path(lock_dir)
        self.log = log
        self._proc: subprocess.Popen | None = None
        self._job = None
        self._replies: queue.Queue[dict | None] = queue.Queue()
        self._stderr: deque[str] = deque(maxlen=STDERR_KEEP)
        self._threads: list[threading.Thread] = []
        self._lock_path: Path | None = None
        self._lock_fd: int | None = None
        self._next_id = 0
        self._dead = False  # killed or exited: never restarted
        self._closed = False
        self._killed = False
        self._mutex = threading.Lock()

    # -- start ---------------------------------------------------------------------------------------------------------

    def start(self, hello: dict) -> dict:
        """Take the GPU lock, spawn the worker, send `hello`; returns the worker's `ready` object."""
        if self._proc is not None or self._closed:
            raise CodecastError("voice worker already started", stage="voice")
        self._acquire_lock()
        try:
            self._spawn()
            reply = self.request("hello", protocol=PROTOCOL, voice=hello)
        except BaseException:
            self.close()
            raise
        ready = reply.get("ready")
        return ready if isinstance(ready, dict) else {}

    def _spawn(self) -> None:
        kwargs: dict = {"stdin": subprocess.PIPE, "stdout": subprocess.PIPE, "stderr": subprocess.PIPE,
                        "env": worker_env(os.environ)}
        if sys.platform == "win32":
            # Suspended until it is in the job: a venv's python.exe starts the real interpreter as a child at once.
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW | _CREATE_SUSPENDED
        else:
            kwargs["start_new_session"] = True
        self.log.info("voice worker: starting %s", subprocess.list2cmdline(self.argv))
        try:
            proc = subprocess.Popen(self.argv, **kwargs)
        except OSError as e:
            self._dead = True
            raise CodecastError(f"cannot start the voice worker {self.argv[0]}: {e}", stage="voice") from e
        self._proc = proc
        _LIVE.add(self)
        if sys.platform == "win32":
            self._job = _make_job(int(proc._handle))
            if self._job is None:
                self.log.warning("voice worker: no Job Object (error %d); its children may outlive CodeCast",
                                 ctypes.get_last_error())
            if not _resume(int(proc._handle)):
                self._kill_tree()
                raise CodecastError("cannot start the voice worker: resuming its process failed", stage="voice")
        for target, stream in ((self._read_stdout, proc.stdout), (self._read_stderr, proc.stderr)):
            t = threading.Thread(target=target, args=(stream,), daemon=True, name=f"voice-worker-{target.__name__}")
            t.start()
            self._threads.append(t)

    def _read_stdout(self, stream) -> None:
        try:
            for raw in stream:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    msg = None
                if not isinstance(msg, dict):
                    self.log.warning("voice worker: ignored a stdout line that is not protocol JSON: %s", line[:200])
                    continue
                if "progress" in msg:
                    self.log.info("voice worker: %s", msg["progress"])
                    continue
                self._replies.put(msg)
        except (OSError, ValueError):
            pass
        finally:
            self._replies.put(None)

    def _read_stderr(self, stream) -> None:
        try:
            for raw in stream:
                line = raw.decode("utf-8", errors="replace").rstrip()
                self.log.debug("voice worker stderr: %s", line[:2000])
                self._stderr.append(line[:2000])
        except (OSError, ValueError):
            pass

    # -- requests ------------------------------------------------------------------------------------------------------

    def request(self, op: str, **fields) -> dict:
        """Send one request and wait for its reply (bounded); any failure is a voice-stage CodecastError."""
        proc = self._proc
        if proc is None or self._dead or self._closed:
            raise self._error(f"voice worker is not running (cannot {op})")
        self._next_id += 1
        rid = self._next_id
        line = (json.dumps({"id": rid, "op": op, **fields}, ensure_ascii=True) + "\n").encode("ascii")
        try:
            proc.stdin.write(line)
            proc.stdin.flush()
        except (OSError, ValueError):
            raise self._died(op) from None
        hello = op == "hello"
        timeout = self.start_timeout_s if hello else self.sentence_timeout_s
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._dead = True
                self._kill_tree()
                self._release_lock()
                if hello:
                    raise self._error(f"voice worker did not start within {timeout:g}s "
                                      "(voice.worker_start_timeout_s); stopped it")
                raise self._error(f"voice worker took longer than {timeout:g}s on {op} "
                                  "(voice.worker_sentence_timeout_s); stopped it")
            try:
                msg = self._replies.get(timeout=min(remaining, 1.0))
            except queue.Empty:
                continue
            if msg is None:
                raise self._died(op)
            mid = msg.get("id")
            if mid is None and not ("id" in msg and "ok" in msg):  # only {"id": null, "ok": ...} is the protocol's
                self.log.debug("voice worker: ignored a stdout object that is not a reply: %s", str(msg)[:200])
                continue
            if mid is not None and mid != rid:
                self.log.debug("voice worker: ignored a reply to request %r (waiting for %d)", mid, rid)
                continue
            if mid is not None and msg.get("ok") is True:
                return msg
            error = msg.get("error") or "the worker answered without ok and without an error"
            if mid is None:
                error = f"malformed request: {error}"
            if hello:
                self._dead = True
            raise self._error(f"voice worker failed on {op}: {error}")

    def _died(self, op: str) -> CodecastError:
        self._dead = True
        proc = self._proc
        code = None
        if proc is not None:
            try:
                code = proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
        if len(self._threads) > 1:
            self._threads[1].join(1)  # let the stderr reader catch the last lines
        self._kill_tree()
        self._release_lock()
        return self._error(f"voice worker exited unexpectedly (exit code {code}) during {op}")

    def _error(self, message: str) -> CodecastError:
        if "cuda out of memory" in message.lower():
            message += " (CUDA out of memory: another CodeCast run, or another program, may hold the GPU)"
        tail = self.stderr_tail()
        if tail:
            message += f"\nworker stderr (last {len(tail)} lines):\n" + "\n".join(tail)
        return CodecastError(message, stage="voice", details={"stderr_tail": tail})

    def stderr_tail(self) -> list[str]:
        return list(self._stderr)

    # -- stop ----------------------------------------------------------------------------------------------------------

    def close(self) -> None:
        """Send `quit`, wait QUIT_WAIT_S, then kill the worker and its children; release the GPU lock."""
        with self._mutex:
            if self._closed:
                return
            self._closed = True
        try:
            proc = self._proc
            if proc is not None:
                if proc.poll() is None and not self._dead:  # a dead (failed, timed-out) worker is killed at once
                    self._next_id += 1
                    try:
                        proc.stdin.write((json.dumps({"id": self._next_id, "op": "quit"}) + "\n").encode("ascii"))
                        proc.stdin.flush()
                    except (OSError, ValueError):
                        pass
                    try:
                        proc.wait(timeout=QUIT_WAIT_S)
                    except subprocess.TimeoutExpired:
                        self.log.warning("voice worker pid %d did not quit within %gs; killing it", proc.pid,
                                         QUIT_WAIT_S)
                self._kill_tree()
                try:
                    proc.stdin.close()
                except (OSError, ValueError):
                    pass
                for t in self._threads:
                    t.join(2)
        finally:
            self._release_lock()
            _LIVE.discard(self)

    def _kill_tree(self) -> None:
        proc = self._proc
        if proc is None or self._killed:
            return
        self._killed = True
        if sys.platform == "win32":
            job, self._job = self._job, None
            if job is not None:
                _close_job(job)
            if proc.poll() is None:
                try:
                    proc.kill()
                except OSError:
                    pass
        else:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.log.warning("voice worker pid %d still running after kill", proc.pid)

    # -- the GPU lock --------------------------------------------------------------------------------------------------

    def _acquire_lock(self) -> None:
        """Hold an OS lock on `<lock_dir>/gpu.lock` for the worker's life; the OS frees it if this process dies."""
        if not self.lock_device or self.lock_device == "cpu":
            return
        self.lock_dir.mkdir(parents=True, exist_ok=True)
        path = self.lock_dir / "gpu.lock"  # R2: one lock for every GPU device
        key = str(path.resolve())
        with _HELD_MUTEX:
            if key in _HELD:
                raise CodecastError(f"an engine in this process still holds the GPU voice lock {path}; close it "
                                    "first", stage="voice")
            _HELD.add(key)  # reserved while we wait, so a second engine here fails at once too
        try:
            try:
                fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o644)
            except OSError as e:
                raise CodecastError(f"cannot create the GPU voice lock {path}: {e}", stage="voice") from e
            try:
                self._wait_for_lock(fd, path)
                data = f"\n{os.getpid()}\n".encode("ascii")  # byte 0 is the locked one; the pid follows it
                os.lseek(fd, 0, os.SEEK_SET)
                os.write(fd, data)
                os.ftruncate(fd, len(data))
            except BaseException:
                os.close(fd)
                raise
        except BaseException:
            with _HELD_MUTEX:
                _HELD.discard(key)
            raise
        self._lock_path, self._lock_fd = path, fd

    def _wait_for_lock(self, fd: int, path: Path) -> None:
        deadline = time.monotonic() + self.start_timeout_s
        announced = object()
        while not _try_lock(fd):
            holder = _lock_holder(path)
            if holder != announced:
                self.log.info("waiting for the voice worker of pid %s (another CodeCast run holds the GPU voice "
                              "lock %s)", holder, path)
                announced = holder
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CodecastError(f"another CodeCast run (pid {holder}) holds the GPU voice lock {path} after "
                                    f"{self.start_timeout_s:g}s (voice.worker_start_timeout_s)", stage="voice")
            time.sleep(min(LOCK_POLL_S, remaining))

    def _release_lock(self) -> None:
        """Unlock and close; the file stays (deleting it would race with other openers)."""
        path, fd = self._lock_path, self._lock_fd
        self._lock_path = self._lock_fd = None
        if fd is None:
            return
        try:
            _unlock(fd)
        except OSError as e:
            self.log.warning("voice worker: could not unlock the GPU voice lock %s: %s", path, e)
        finally:
            os.close(fd)
            with _HELD_MUTEX:
                _HELD.discard(str(path.resolve()))


def _close_all() -> None:
    for w in list(_LIVE):
        try:
            w.close()
        except Exception:  # noqa: BLE001 - at exit, close what we can
            pass


atexit.register(_close_all)
