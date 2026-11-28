"""`codecast voice-env` (spec 5.6): the commands it runs, the torch default, the env lock. uv is never run."""
from __future__ import annotations

import subprocess
import sys
from types import SimpleNamespace

import pytest

from codecast.config import VoiceConfig
from codecast.errors import CodecastError, ConfigError
from codecast.voice import envs
from codecast.voice.model_engine import interpreter_for
from codecast.voice.voicefile import WORKERS_DIR


class Recorder:
    def __init__(self, *, nvidia: bool = True, fail: str | None = None):
        self.calls: list[list[str]] = []
        self.nvidia = nvidia
        self.fail = fail

    def __call__(self, argv, **kw):
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        if argv[0] == "nvidia-smi":
            if not self.nvidia:
                raise FileNotFoundError("nvidia-smi")
            return SimpleNamespace(returncode=0)
        if self.fail and self.fail in argv:
            return SimpleNamespace(returncode=2)
        return SimpleNamespace(returncode=0)

    @property
    def uv(self) -> list[list[str]]:
        return [c for c in self.calls if c[0] != "nvidia-smi"]


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setattr(envs.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(envs.sys, "platform", "win32")
    return VoiceConfig(envs_dir=str(tmp_path / "envs"))


def test_create_env_commands(cfg, tmp_path):
    run = Recorder()
    interp = envs.create_env("omnivoice", cfg, torch="cu128", runner=run)
    env = tmp_path / "envs" / "omnivoice"
    assert interp == interpreter_for("omnivoice", cfg)
    assert run.uv[0] == ["uv", "venv", "--python", "3.12", str(env)]
    installs = run.uv[1:]
    assert all(c[:4] == ["uv", "pip", "install", "--python"] and c[4] == str(interp) for c in installs)
    torch_cmd = next(c for c in installs if "torch==2.8.0+cpu" in c or "torch==2.8.0+cu128" in c)
    assert "torchaudio==2.8.0+cu128" in torch_cmd
    assert torch_cmd[torch_cmd.index("--index-url") + 1] == "https://download.pytorch.org/whl/cu128"
    req = next(c for c in installs if "-r" in c)
    assert req[req.index("-r") + 1] == str(WORKERS_DIR / "omnivoice.requirements.txt")
    assert "--index-url" not in req
    assert installs.index(torch_cmd) < installs.index(req)  # torch first, so the requirements keep it


def test_cpu_index(cfg):
    run = Recorder()
    envs.create_env("omnivoice", cfg, torch="cpu", runner=run)
    cmd = next(c for c in run.uv if "torch==2.8.0+cpu" in c or "torch==2.8.0+cu128" in c)
    assert cmd[cmd.index("--index-url") + 1] == "https://download.pytorch.org/whl/cpu"


def test_mac_uses_pypi_torch(cfg, monkeypatch):
    monkeypatch.setattr(envs.sys, "platform", "darwin")
    run = Recorder()
    envs.create_env("omnivoice", cfg, torch=None, runner=run)
    cmd = next(c for c in run.uv if "torch==2.8.0" in c)
    assert "--index-url" not in cmd
    assert "torch==2.8.0" in cmd and "--reinstall-package" not in cmd
    assert not any(c[0] == "nvidia-smi" for c in run.calls)


def test_default_torch_cu128_when_nvidia_smi(cfg):
    run = Recorder(nvidia=True)
    envs.create_env("omnivoice", cfg, torch=None, runner=run)
    cmd = next(c for c in run.uv if "torch==2.8.0+cpu" in c or "torch==2.8.0+cu128" in c)
    assert cmd[cmd.index("--index-url") + 1].endswith("/cu128")


def test_default_torch_cpu_without_nvidia_smi(cfg):
    run = Recorder(nvidia=False)
    envs.create_env("omnivoice", cfg, torch=None, runner=run)
    cmd = next(c for c in run.uv if "torch==2.8.0+cpu" in c or "torch==2.8.0+cu128" in c)
    assert cmd[cmd.index("--index-url") + 1].endswith("/cpu")


def test_mlx_audio_has_no_torch(cfg, monkeypatch):
    monkeypatch.setattr(envs.sys, "platform", "darwin")
    run = Recorder()
    envs.create_env("mlx-audio", cfg, torch=None, runner=run)
    assert not any(any(a.startswith("torch==") for a in c) for c in run.uv)
    assert any("mlx-audio.requirements.txt" in " ".join(c) for c in run.uv)


def test_uv_missing_is_config_error(cfg, monkeypatch):
    monkeypatch.setattr(envs.shutil, "which", lambda name: None)
    with pytest.raises(ConfigError, match="uv"):
        envs.create_env("omnivoice", cfg, torch="cpu", runner=Recorder())


def test_unknown_worker(cfg):
    with pytest.raises(ConfigError, match="unknown worker 'nope'"):
        envs.create_env("nope", cfg, torch=None, runner=Recorder())


def test_bad_torch_choice(cfg):
    with pytest.raises(ConfigError, match="--torch"):
        envs.create_env("omnivoice", cfg, torch="cu999", runner=Recorder())


def test_failed_step_is_an_error(cfg):
    with pytest.raises(CodecastError, match="exit 2"):
        envs.create_env("omnivoice", cfg, torch="cpu", runner=Recorder(fail="venv"))


HOLDER = """
import sys, time
from pathlib import Path
from codecast.config import VoiceConfig
from codecast.voice import envs
cfg = VoiceConfig(envs_dir=sys.argv[1])
with envs.env_lock("omnivoice", cfg):
    print("held", flush=True)
    sys.stdin.readline()
"""


def test_env_lock_refuses_second_builder(cfg, tmp_path):
    p = subprocess.Popen([sys.executable, "-c", HOLDER, str(tmp_path / "envs")], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, text=True)
    try:
        assert p.stdout.readline().strip() == "held"
        run = Recorder()
        with pytest.raises(CodecastError, match="already being built"):
            envs.create_env("omnivoice", cfg, torch="cpu", runner=run)
        assert run.uv == []
    finally:
        p.stdin.write("\n")
        p.stdin.close()
        p.wait(timeout=20)
    envs.create_env("omnivoice", cfg, torch="cpu", runner=Recorder())  # released: a new build works


def test_switching_torch_builds_reinstalls_torch(cfg):
    run = Recorder()
    envs.create_env("omnivoice", cfg, torch="cpu", runner=run)
    cpu = next(c for c in run.uv if "torch==2.8.0+cpu" in c)
    assert "torchaudio==2.8.0+cpu" in cpu
    assert [cpu[i + 1] for i, a in enumerate(cpu) if a == "--reinstall-package"] == ["torch", "torchaudio"]
    run = Recorder()
    envs.create_env("omnivoice", cfg, torch="cu128", runner=run)
    assert any("torch==2.8.0+cu128" in c and "--reinstall-package" in c for c in run.uv)


def test_nvidia_smi_timeout_means_cpu():
    def hang(argv, **kw):
        raise subprocess.TimeoutExpired(argv, kw["timeout"])

    assert envs.default_torch(hang) == "cpu"
