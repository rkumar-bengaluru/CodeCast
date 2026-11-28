"""The worker client (spec 5.4): process life, pipes, environment, the GPU lock. Driven through the fake worker."""
from __future__ import annotations

import json
import logging
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from codecast.errors import CodecastError
from codecast.voice import worker as worker_mod
from codecast.voice.worker import Worker, worker_env

FAKE = Path(__file__).resolve().parent.parent / "fake_worker.py"
WORKERS = Path(__file__).resolve().parents[2] / "src" / "codecast" / "voice" / "workers"
T = 20  # seconds: every wait in these tests is bounded
log = logging.getLogger("codecast.voice.test")


def make(tmp_path, argv=None, *, start=30.0, sentence=30.0, lock_device=None) -> Worker:
    return Worker(argv or [sys.executable, str(FAKE)], start_timeout_s=start, sentence_timeout_s=sentence,
                  lock_device=lock_device, lock_dir=tmp_path / "locks", log=log)


def hello(**options) -> dict:
    return {"worker": None, "model": "fake", "device": "cpu", "options": options}


def wait_gone(pid: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not worker_mod._pid_alive(pid):
            return True
        time.sleep(0.1)
    return False


def script(tmp_path, body: str) -> list[str]:
    p = tmp_path / "w.py"
    p.write_text(body, encoding="utf-8")
    return [sys.executable, str(p)]


def test_start_returns_ready_and_request_round_trips(tmp_path):
    w = make(tmp_path)
    try:
        ready = w.start(hello())
        assert ready["worker"] == "fake"
        out = tmp_path / "o.wav"
        assert w.request("synthesize", text="hi", seed=1, out=str(out))["ok"] is True
        assert out.is_file()
    finally:
        w.close()


def test_close_quits(tmp_path):
    w = make(tmp_path)
    w.start(hello())
    proc = w._proc
    t0 = time.monotonic()
    w.close()
    assert time.monotonic() - t0 < 5
    assert proc.poll() is not None
    w.close()  # a second close is harmless


def test_close_kills_when_quit_ignored(tmp_path):
    w = make(tmp_path)
    w.start(hello(ignore_quit=True))
    proc = w._proc
    t0 = time.monotonic()
    w.close()
    took = time.monotonic() - t0
    assert 9.5 <= took < 20, took  # waited the 10 s for quit, then killed it
    assert proc.poll() is not None


def test_worker_exits_on_stdin_eof_while_busy(tmp_path):
    w = make(tmp_path)
    w.start(hello(sleep_synth=60))
    errors: list[BaseException] = []

    def busy():
        try:
            w.request("synthesize", text="slow", seed=1, out=str(tmp_path / "o.wav"))
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    t = threading.Thread(target=busy, daemon=True)
    t.start()
    time.sleep(1)
    w._proc.stdin.close()  # CodeCast's end of the pipe is gone
    try:
        w._proc.wait(timeout=5)
    finally:
        t.join(T)
        w.close()
    assert w._proc.poll() is not None
    assert errors and isinstance(errors[0], CodecastError)


@pytest.mark.skipif(sys.platform != "win32", reason="Job Objects are Windows only")
def test_job_object_kills_children(tmp_path):
    import ctypes
    pid_file = tmp_path / "child.pid"
    w = make(tmp_path)
    try:
        w.start(hello(spawn_child=str(pid_file)))
        child = int(pid_file.read_text())
        assert worker_mod._pid_alive(child)
        job, w._job = w._job, None
        ctypes.WinDLL("kernel32").CloseHandle(job)  # what happens when CodeCast dies: the last job handle closes
        assert wait_gone(child), "grandchild survived the job"
        w._proc.wait(timeout=5)
    finally:
        w.close()


@pytest.mark.skipif(sys.platform == "win32", reason="process groups are POSIX only")
def test_process_group_kills_children(tmp_path):
    pid_file = tmp_path / "child.pid"
    w = make(tmp_path)
    try:
        w.start(hello(spawn_child=str(pid_file)))
        child = int(pid_file.read_text())
        assert worker_mod._pid_alive(child)
    finally:
        w.close()
    assert wait_gone(child), "grandchild survived the process group kill"


def test_stderr_flood_does_not_block(tmp_path):
    w = make(tmp_path, start=T)
    try:
        assert w.start(hello(stderr_flood=5_000_000))["worker"] == "fake"
    finally:
        w.close()


def test_stdout_noise_is_ignored_and_stderr_kept(tmp_path):
    w = make(tmp_path)
    try:
        w.start(hello(stdout_noise=True))
        w.request("synthesize", text="hi", seed=1, out=str(tmp_path / "o.wav"))
        assert "noise from a native write" in w.stderr_tail()
    finally:
        w.close()


def test_env_allowlist(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-real")
    monkeypatch.setenv("HF_HOME", str(tmp_path / "hf"))
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "0")
    env = worker_env({"ANTHROPIC_API_KEY": "x", "HF_HOME": "h", "PATH": "p", "OTHER": "o", "torch_home": "t"})
    assert env == {"HF_HOME": "h", "PATH": "p", "torch_home": "t", "PYTHONUTF8": "1",
                   "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
    dump = tmp_path / "env.json"
    code = (f"import json, os, runpy, sys; open(r'{dump}', 'w').write(json.dumps(dict(os.environ))); "
            f"sys.argv = [r'{FAKE}']; runpy.run_path(r'{FAKE}', run_name='__main__')")
    w = make(tmp_path, [sys.executable, "-c", code])
    try:
        w.start(hello())
    finally:
        w.close()
    seen = {k.upper(): v for k, v in json.loads(dump.read_text()).items()}
    assert "ANTHROPIC_API_KEY" not in seen
    assert seen["HF_HOME"] == str(tmp_path / "hf")
    assert seen["CUDA_VISIBLE_DEVICES"] == "0"
    assert seen["PYTHONUTF8"] == "1"


def test_gpu_lock_waits(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="codecast")
    a = make(tmp_path, lock_device="cuda")
    b = make(tmp_path, lock_device="cuda:0")  # R2: every non-cpu device shares the one `gpu` lock
    started: list[dict] = []
    t = None
    try:
        a.start(hello())
        assert (tmp_path / "locks" / "gpu.lock").is_file()
        t = threading.Thread(target=lambda: started.append(b.start(hello())), daemon=True)
        t.start()
        time.sleep(2.5)
        assert not started and b._proc is None
        assert "waiting for the voice worker of pid" in caplog.text
        a.close()
        t.join(T)
        assert started
    finally:
        a.close()
        if t is not None:
            t.join(T)
        b.close()
    assert not (tmp_path / "locks" / "gpu.lock").exists()


def test_cpu_takes_no_lock(tmp_path):
    w = make(tmp_path, lock_device="cpu")
    try:
        w.start(hello())
        assert not (tmp_path / "locks" / "gpu.lock").exists()
    finally:
        w.close()


def test_stale_lock_is_removed(tmp_path):
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait(timeout=T)
    (tmp_path / "locks").mkdir()
    (tmp_path / "locks" / "gpu.lock").write_text(str(dead.pid))
    w = make(tmp_path, lock_device="cuda", start=10)
    try:
        w.start(hello())
        assert (tmp_path / "locks" / "gpu.lock").read_text().strip() == str(worker_mod.os.getpid())
    finally:
        w.close()


def test_lock_wait_is_bounded_by_start_timeout(tmp_path):
    (tmp_path / "locks").mkdir()
    (tmp_path / "locks" / "gpu.lock").write_text(str(worker_mod.os.getpid()))  # held by a live pid
    w = make(tmp_path, lock_device="cuda", start=1.5)
    with pytest.raises(CodecastError) as e:
        w.start(hello())
    assert e.value.stage == "voice" and "pid" in e.value.message
    assert w._proc is None


def test_null_id_reply_fails_the_request_in_flight(tmp_path):
    argv = script(tmp_path, "import sys\nsys.stdin.readline()\n"
                            "sys.stdout.write('not json at all\\n')\n"
                            "sys.stdout.write('{\"id\": null, \"ok\": false, \"error\": \"bad request line: x\"}\\n')\n"
                            "sys.stdout.flush()\nimport time\ntime.sleep(60)\n")
    w = make(tmp_path, argv)
    t0 = time.monotonic()
    with pytest.raises(CodecastError) as e:
        w.start(hello())
    assert "bad request line" in e.value.message
    assert time.monotonic() - t0 < T
    w.close()


def test_start_timeout(tmp_path):
    w = make(tmp_path, start=1.5)
    with pytest.raises(CodecastError) as e:
        w.start(hello(sleep_load=60))
    assert e.value.stage == "voice" and "start" in e.value.message
    assert w._proc.poll() is not None


def test_worker_error_reply_carries_message(tmp_path):
    w = make(tmp_path)
    try:
        w.start(hello(fail_design=True))
        with pytest.raises(CodecastError) as e:
            w.request("design", text="x", language="en", instruct="calm", seed=1, out=str(tmp_path / "r.wav"))
        assert "fake worker cannot design" in e.value.message
        w.request("synthesize", text="still serving", seed=1, out=str(tmp_path / "o.wav"))
    finally:
        w.close()


def test_atexit_closes_live_workers(tmp_path):
    w = make(tmp_path)
    w.start(hello())
    assert w in worker_mod._LIVE
    worker_mod._close_all()
    assert w._proc.poll() is not None and w not in worker_mod._LIVE


def test_cuda_oom_hint(tmp_path):
    argv = script(tmp_path, f"import sys\nsys.path.insert(0, r'{WORKERS}')\nimport _protocol\n_protocol.setup_streams()\n"
                            "class H:\n"
                            "    def hello(self, voice):\n        return {'worker': 'oom'}\n"
                            "    def synthesize(self, text, seed, out):\n"
                            "        raise RuntimeError('CUDA out of memory. Tried to allocate 2.00 GiB')\n"
                            "_protocol.serve(H())\n")
    w = make(tmp_path, argv)
    try:
        w.start(hello())
        with pytest.raises(CodecastError) as e:
            w.request("synthesize", text="x", seed=1, out=str(tmp_path / "o.wav"))
    finally:
        w.close()
    assert "CUDA out of memory" in e.value.message
    assert "another" in e.value.message and "GPU" in e.value.message
