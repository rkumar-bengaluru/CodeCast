"""Worker side of the voice protocol (spec 5.5): stdlib only, imported by workers from their own folder.

Never import codecast here: a worker's environment does not have it.
"""
from __future__ import annotations

import json
import os
import queue
import sys
import threading
import traceback

PROTOCOL = 1

_proto_fd: int | None = None
_stdin = None
_current_id: int | None = None
_lock = threading.Lock()


def setup_streams() -> None:
    """Keep the real stdout for the protocol and point fd 1 (and sys.stdout) at stderr. Call before any model import."""
    global _proto_fd, _stdin
    if _proto_fd is not None:
        return
    _stdin = sys.stdin.buffer
    sys.stdout.flush()
    _proto_fd = os.dup(1)
    if sys.platform == "win32":
        import msvcrt

        msvcrt.setmode(_proto_fd, os.O_BINARY)
    os.dup2(2, 1)
    sys.stdout = open(1, "w", encoding="utf-8", errors="replace", buffering=1, closefd=False)  # noqa: SIM115


def _send(obj: dict) -> None:
    data = (json.dumps(obj, ensure_ascii=True) + "\n").encode("ascii")
    with _lock:
        view = memoryview(data)
        while view:
            view = view[os.write(_proto_fd, view):]


def progress(msg: str) -> None:
    """Report progress for the request in hand."""
    _send({"id": _current_id, "progress": msg})


def _watch_stdin(q: queue.Queue) -> None:
    while True:
        line = _stdin.readline()
        if not line:
            os._exit(0)  # CodeCast is gone or closed us: end now, even mid-load or mid-sentence
        q.put(line)


def _exit(code: int) -> None:
    try:
        sys.stderr.flush()
    finally:
        os._exit(code)


def serve(handler) -> None:
    """Answer requests one at a time until `quit` or stdin EOF."""
    global _current_id
    if _proto_fd is None:
        setup_streams()
    q: queue.Queue = queue.Queue()
    threading.Thread(target=_watch_stdin, args=(q,), daemon=True).start()
    ready = False
    while True:
        line = q.get()
        try:
            req = json.loads(line.decode("utf-8"))
            if not isinstance(req, dict):
                raise ValueError("request is not an object")
        except ValueError as e:
            print(f"worker: bad request line: {e}", file=sys.stderr, flush=True)
            _send({"id": None, "ok": False, "error": f"bad request line: {e}"})
            continue
        _current_id = rid = req.get("id")
        op = req.get("op")
        try:
            if op == "hello":
                if type(req.get("protocol")) is not int or req["protocol"] != PROTOCOL:
                    _send({"id": rid, "ok": False, "error": f"protocol {req.get('protocol')!r} is not supported;"
                                                            f" this worker speaks protocol {PROTOCOL}"})
                    _exit(1)
                ready_obj = handler.hello(req.get("voice") or {})
                ready = True
                _send({"id": rid, "ok": True, "ready": ready_obj})
            elif not ready:
                _send({"id": rid, "ok": False, "error": "hello must be the first request"})
                _exit(1)
            elif op == "synthesize":
                handler.synthesize(req["text"], int(req["seed"]), req["out"])
                _send({"id": rid, "ok": True})
            elif op == "design":
                handler.design(req["text"], req.get("language"), req.get("instruct"), int(req["seed"]), req["out"])
                _send({"id": rid, "ok": True})
            elif op == "quit":
                _send({"id": rid, "ok": True})
                _exit(0)
            else:
                _send({"id": rid, "ok": False, "error": f"unknown op {op!r}"})
        except Exception as e:  # noqa: BLE001 - every handler failure is a reply; the worker keeps serving
            traceback.print_exc(file=sys.stderr)
            _send({"id": rid, "ok": False, "error": f"{type(e).__name__}: {e}"})
            if op == "hello":
                _exit(1)
