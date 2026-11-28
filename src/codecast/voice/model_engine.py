"""The `model` engine (spec 5.3, 5.4): a voice file voiced by a long-lived worker process, started lazily."""
from __future__ import annotations

import logging
import shutil
import sys
import unicodedata
from pathlib import Path

from codecast.config import VoiceConfig, resolved_voice
from codecast.errors import CodecastError, ConfigError
from codecast.voice import registry, voicefile
from codecast.voice.engines import TTSEngine, convert_to_wav48k
from codecast.voice.voicefile import ResolvedVoice, load_voice_file, sentence_seed
from codecast.voice.worker import Worker

log = logging.getLogger("codecast.voice.worker")


def interpreter_for(worker: str, cfg: VoiceConfig) -> Path:
    """A shipped worker's Python: `voice.worker_python.<worker>`, else the environment `voice-env` builds."""
    if worker in cfg.worker_python:
        return Path(cfg.worker_python[worker]).expanduser()
    env = Path(cfg.envs_dir).expanduser() / worker
    return env / "Scripts" / "python.exe" if sys.platform == "win32" else env / "bin" / "python"


def worker_argv(voice: ResolvedVoice, cfg: VoiceConfig) -> list[str]:
    if voice.command is not None:
        return list(voice.command)
    return [str(interpreter_for(voice.worker, cfg)), str(voicefile.WORKERS_DIR / voicefile.SHIPPED_WORKERS[voice.worker])]


def lock_dir(cfg: VoiceConfig) -> Path:
    return Path(cfg.envs_dir).expanduser() / "locks"


class ModelEngine:
    name = "model"

    def __init__(self, voice: ResolvedVoice, cfg: VoiceConfig):
        self.voice = voice
        self.voice_id = voice.voice_id
        self.cfg = cfg
        self.ready: dict | None = None
        self._worker: Worker | None = None

    def _started(self) -> Worker:
        if self._worker is None:
            self._worker = Worker(worker_argv(self.voice, self.cfg),
                                  start_timeout_s=self.cfg.worker_start_timeout_s,
                                  sentence_timeout_s=self.cfg.worker_sentence_timeout_s,
                                  lock_device=None if self.voice.device == "cpu" else self.voice.device,
                                  lock_dir=lock_dir(self.cfg), log=log)
            self.ready = self._worker.start(self.voice.to_hello())
            log.info("voice worker ready: %s", self.ready)
        return self._worker

    def synthesize(self, text: str, out_wav: Path) -> None:
        text = unicodedata.normalize("NFC", text)
        out_wav = Path(out_wav)
        raw = out_wav.with_name(out_wav.stem + ".raw.wav")
        try:
            self._started().request("synthesize", text=text, seed=sentence_seed(text), out=str(raw))
            convert_to_wav48k(raw, out_wav)
        except CodecastError as e:
            raise CodecastError(f"voice worker failed for {text[:80]!r}: {e.message}", stage="voice",
                                details=e.details) from e
        finally:
            raw.unlink(missing_ok=True)

    def close(self) -> None:
        """Stop the worker; never raises (a raising close would replace the run's own result)."""
        if self._worker is None:
            return
        try:
            self._worker.close()  # the closed worker stays: a later synthesize fails rather than restarting it
        except Exception:  # noqa: BLE001
            log.warning("closing the voice worker failed", exc_info=True)


def _program_exists(program: str) -> bool:
    return shutil.which(program) is not None or Path(program).is_file()


class _ModelKind:
    name = "model"

    def status(self, voice_part: str, cfg: VoiceConfig, *, allow_network: bool) -> str:
        """Without starting the worker (spec 5.7): the file parses, its reference and its program exist."""
        if not voice_part:
            named = resolved_voice(cfg)
            if named is None or not named.startswith("model:"):
                return "ok"
            voice_part = named.partition(":")[2]
        try:
            voice = load_voice_file(Path(voice_part).expanduser())
        except ConfigError as e:
            return e.message
        if voice.command is not None:
            if not _program_exists(voice.command[0]):
                return f"voice file {voice.path}: command program {voice.command[0]!r} not found"
            return "ok"
        py = interpreter_for(voice.worker, cfg)
        if not py.is_file():
            return (f"worker {voice.worker!r}: interpreter not found at {py}; run `codecast voice-env {voice.worker}`"
                    f" or set voice.worker_python.{voice.worker}")
        return "ok"

    def create(self, voice_part: str, cfg: VoiceConfig) -> TTSEngine:
        return ModelEngine(load_voice_file(Path(voice_part).expanduser()), cfg)


registry.register(_ModelKind())
