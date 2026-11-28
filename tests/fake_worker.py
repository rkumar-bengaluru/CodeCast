"""A fake voice worker for tests: speaks the protocol, writes a 0.5 s 24 kHz sine per sentence.

Behaviour switches come from the voice's `options`: progress, sleep_load, sleep_synth, crash_on (sentence text),
stdout_noise, stderr_flood (bytes), ignore_quit, fail_design, spawn_child (path that gets the grandchild's pid).
"""
from __future__ import annotations

import math
import os
import struct
import subprocess
import sys
import time
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src" / "codecast" / "voice" / "workers"))
import _protocol  # noqa: E402

_protocol.setup_streams()


def _write_wav(out: str) -> None:
    rate = 24000
    frames = b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * i / rate))) for i in range(rate // 2))
    with wave.open(out, "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(rate)
        f.writeframes(frames)


class _HungOs:
    """`os` as the protocol sees it, where a clean exit never happens (quit and stdin EOF both hang)."""

    def __getattr__(self, name):
        return getattr(os, name)

    @staticmethod
    def _exit(code):
        while code == 0:
            time.sleep(60)
        os._exit(code)


def _hang() -> None:
    """Model a hung worker that ignores both `quit` and stdin EOF."""
    _protocol.os = _HungOs()


class Handler:
    options: dict = {}

    def hello(self, voice: dict) -> dict:
        self.options = o = voice.get("options") or {}
        if o.get("ignore_quit"):
            _hang()
        if o.get("progress"):
            _protocol.progress(str(o["progress"]))
        if o.get("sleep_load"):
            time.sleep(float(o["sleep_load"]))
        if o.get("stdout_noise"):
            print("noise from print")
            os.write(1, b"noise from a native write\n")
        if o.get("stderr_flood"):
            sys.stderr.buffer.write(b"e" * int(o["stderr_flood"]))
            sys.stderr.buffer.flush()
        if o.get("spawn_child"):
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
            Path(str(o["spawn_child"])).write_text(str(child.pid))
        return {"worker": "fake", "model": "fake", "device": "cpu", "versions": {}}

    def synthesize(self, text: str, seed: int, out: str) -> None:
        o = self.options
        if o.get("crash_on") is not None and text == o["crash_on"]:
            os._exit(3)
        if o.get("stdout_noise"):
            print("noise during synth")
        if o.get("sleep_synth"):
            time.sleep(float(o["sleep_synth"]))
        _write_wav(out)

    def design(self, text, language, instruct, seed, out) -> None:
        if self.options.get("fail_design"):
            raise NotImplementedError("fake worker cannot design")
        _write_wav(out)


if __name__ == "__main__":
    _protocol.serve(Handler())
