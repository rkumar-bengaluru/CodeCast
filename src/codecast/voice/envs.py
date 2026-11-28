"""`codecast voice-env`: build a shipped worker's own uv environment (spec 5.6)."""
from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

from codecast.config import VoiceConfig
from codecast.errors import CodecastError, ConfigError
from codecast.voice import voicefile, worker
from codecast.voice.model_engine import interpreter_for

TORCH_PINS = ["torch==2.8.0", "torchaudio==2.8.0"]
TORCH_INDEXES = {"cu128": "https://download.pytorch.org/whl/cu128", "cpu": "https://download.pytorch.org/whl/cpu"}
TORCH_WORKERS = {"omnivoice"}  # the shipped workers that run on PyTorch (mlx-audio's model runs on MLX)
PYTHON = "3.12"


def default_torch(runner=subprocess.run) -> str:
    """`cu128` when an NVIDIA driver answers, else `cpu`."""
    try:
        r = runner(["nvidia-smi"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    except OSError:
        return "cpu"
    return "cu128" if r.returncode == 0 else "cpu"


@contextlib.contextmanager
def env_lock(name: str, cfg: VoiceConfig) -> Iterator[None]:
    """`<envs_dir>/locks/env-<name>.lock`, an OS lock: a second builder fails at once; the OS frees it on a crash."""
    lock_dir = Path(cfg.envs_dir).expanduser() / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    path = lock_dir / f"env-{name}.lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o644)
    try:
        if not worker._try_lock(fd):
            raise CodecastError(f"the {name} environment is already being built by another codecast voice-env "
                                f"(lock {path}); wait for it to finish", stage="voice")
        try:
            yield
        finally:
            worker._unlock(fd)
    finally:
        os.close(fd)


def _step(runner, argv: list[str]) -> None:
    print("+ " + subprocess.list2cmdline([str(a) for a in argv]), flush=True)
    try:
        r = runner(argv, check=False)
    except OSError as e:
        raise CodecastError(f"cannot run {argv[0]}: {e}", stage="voice") from e
    if r.returncode != 0:
        raise CodecastError(f"voice-env: `{' '.join(map(str, argv[:3]))}` failed (exit {r.returncode})", stage="voice")


def create_env(name: str, cfg: VoiceConfig, *, torch: str | None, runner=subprocess.run) -> Path:
    """Create (or complete) `<envs_dir>/<name>/` and return its interpreter."""
    if name not in voicefile.SHIPPED_WORKERS:
        raise ConfigError(f"unknown worker {name!r}; shipped: {', '.join(voicefile.SHIPPED_WORKERS)}")
    if torch is not None and torch not in TORCH_INDEXES:
        raise ConfigError(f"--torch must be one of {', '.join(TORCH_INDEXES)}, not {torch!r}")
    if shutil.which("uv") is None:
        raise ConfigError("uv is not on PATH; install it (https://docs.astral.sh/uv/) to build a voice environment")
    req = voicefile.WORKERS_DIR / f"{name}.requirements.txt"
    if not req.is_file():
        raise ConfigError(f"the requirements file {req.name} is missing from {voicefile.WORKERS_DIR}")
    interp = interpreter_for(name, cfg)
    env = Path(cfg.envs_dir).expanduser() / name
    with env_lock(name, cfg):
        if not interp.is_file():
            _step(runner, ["uv", "venv", "--python", PYTHON, str(env)])
        pip = ["uv", "pip", "install", "--python", str(interp)]
        if name in TORCH_WORKERS:
            if sys.platform == "darwin":
                _step(runner, [*pip, *TORCH_PINS])  # PyPI's Mac torch has MPS
            else:
                _step(runner, [*pip, *TORCH_PINS, "--index-url", TORCH_INDEXES[torch or default_torch(runner)]])
        _step(runner, [*pip, "-r", str(req)])
    return interp
