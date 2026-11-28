"""The worker side of the voice protocol, driven through the fake worker with a raw subprocess (no client)."""
from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading
import wave
from pathlib import Path

import pytest

FAKE = Path(__file__).resolve().parent.parent / "fake_worker.py"
T = 20  # seconds: every read is bounded so a bug cannot hang the suite


class Worker:
    def __init__(self, options: dict | None = None) -> None:
        self.options = options or {}
        self.p = subprocess.Popen([sys.executable, str(FAKE)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE)
        self.raw: list[bytes] = []
        self.q: queue.Queue[bytes | None] = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()
        threading.Thread(target=self.p.stderr.read, daemon=True).start()  # drain, so a full pipe cannot block it
        self._n = 0

    def _pump(self) -> None:
        for line in self.p.stdout:
            self.raw.append(line)
            self.q.put(line)
        self.q.put(None)

    def send(self, op: str, **kw) -> int:
        self._n += 1
        line = json.dumps({"id": self._n, "op": op, **kw}, ensure_ascii=True) + "\n"
        self.p.stdin.write(line.encode("ascii"))
        self.p.stdin.flush()
        return self._n

    def reply(self, rid: int) -> dict:
        while True:
            line = self.q.get(timeout=T)
            assert line is not None, "worker closed stdout"
            msg = json.loads(line)
            if msg.get("id") == rid and "progress" not in msg:
                return msg

    def hello(self, protocol: int = 1) -> dict:
        voice = {"worker": "fake", "options": self.options}
        return self.reply(self.send("hello", protocol=protocol, voice=voice))

    def close(self) -> None:
        try:
            self.p.stdin.close()
        except OSError:
            pass
        try:
            self.p.wait(timeout=T)
        except subprocess.TimeoutExpired:
            self.p.kill()


@pytest.fixture()
def make():
    made: list[Worker] = []

    def _make(options=None):
        w = Worker(options)
        made.append(w)
        return w

    yield _make
    for w in made:
        w.close()


def test_hello_synthesize_quit(make, tmp_path):
    w = make()
    r = w.hello()
    assert r["ok"] is True and r["ready"]["worker"] == "fake"
    out = tmp_path / "a.wav"
    assert w.reply(w.send("synthesize", text="hello", seed=1, out=str(out)))["ok"] is True
    with wave.open(str(out)) as f:
        assert f.getnchannels() == 1 and f.getsampwidth() == 2 and f.getframerate() == 24000
        assert f.getnframes() == 12000
    assert w.reply(w.send("quit"))["ok"] is True
    assert w.p.wait(timeout=T) == 0


def test_hindi_text_round_trips(make, tmp_path):
    w = make()
    w.hello()
    out = tmp_path / "hi.wav"
    r = w.reply(w.send("synthesize", text="नमस्ते दुनिया", seed=2,
                       out=str(out)))
    assert r["ok"] is True and out.exists()


def test_stray_stdout_does_not_corrupt_protocol(make, tmp_path):
    w = make({"stdout_noise": True})
    assert w.hello()["ok"] is True
    assert w.reply(w.send("synthesize", text="x", seed=1, out=str(tmp_path / "n.wav")))["ok"] is True
    w.reply(w.send("quit"))
    w.p.wait(timeout=T)
    assert w.raw
    for line in w.raw:
        json.loads(line)  # every protocol line parses


def test_wrong_protocol_version_refused(make):
    w = make()
    r = w.hello(protocol=2)
    assert r["ok"] is False and "protocol" in r["error"]
    assert w.p.wait(timeout=T) == 1


def test_unknown_op_is_ok_false(make):
    w = make()
    w.hello()
    r = w.reply(w.send("dance"))
    assert r["ok"] is False and "dance" in r["error"]
    assert w.reply(w.send("quit"))["ok"] is True  # still serving


def test_hello_must_come_first(make, tmp_path):
    w = make()
    r = w.reply(w.send("synthesize", text="x", seed=1, out=str(tmp_path / "z.wav")))
    assert r["ok"] is False
    assert w.p.wait(timeout=T) == 1


def test_handler_exception_keeps_serving(make):
    w = make({"fail_design": True})
    w.hello()
    r = w.reply(w.send("design", text="x", language="hi", instruct="y", seed=1, out="z"))
    assert r["ok"] is False and r["error"].startswith("NotImplementedError")
    assert w.reply(w.send("quit"))["ok"] is True


def test_ascii_only_replies(make, tmp_path):
    w = make()
    w.hello()
    w.reply(w.send("synthesize", text="नमस्ते", seed=1, out=str(tmp_path / "é.wav")))
    w.reply(w.send("nope-é"))
    w.reply(w.send("quit"))
    w.p.wait(timeout=T)
    assert w.raw
    for line in w.raw:
        line.decode("ascii")


def test_stdin_eof_ends_worker_even_while_loading(make):
    w = make({"sleep_load": 60})
    w.send("hello", protocol=1, voice={"options": {"sleep_load": 60}})
    w.p.stdin.close()
    assert w.p.wait(timeout=T) == 0


def test_progress_lines_carry_request_id(make):
    w = make({"progress": "loading"})
    rid = w.send("hello", protocol=1, voice={"options": {"progress": "loading"}})
    seen = []
    while True:
        msg = json.loads(w.q.get(timeout=T))
        seen.append(msg)
        if "ok" in msg:
            break
    assert {"id": rid, "progress": "loading"} in seen
