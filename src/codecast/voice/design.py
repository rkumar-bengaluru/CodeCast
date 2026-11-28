"""Designing a voice (spec 5.8): ask a worker to invent a voice, keep the clip and write the whole voice file."""
from __future__ import annotations

import logging
from pathlib import Path

import yaml

from codecast.config import VoiceConfig
from codecast.errors import ConfigError
from codecast.voice.model_engine import lock_dir, worker_argv
from codecast.voice.voicefile import ResolvedVoice, load_voice_file, sentence_seed
from codecast.voice.worker import Worker

log = logging.getLogger("codecast.voice.worker")


def design_voice(out: Path, *, worker: str | None = None, model: str, language: str, instruct: str, text: str,
                 options: dict, seed: int | None = None, cfg: VoiceConfig, force: bool = False,
                 device: str = "auto", command: list[str] | None = None) -> ResolvedVoice:
    """Write `<out>` and `<stem>.wav` beside it; refuses to overwrite either without `force`."""
    if (worker is None) == (command is None):
        raise ConfigError("voice design needs exactly one of a worker and a command")
    out = Path(out)
    clip = out.with_suffix(".wav")
    for target in (out, clip):
        if target.exists() and not force:
            raise ConfigError(f"{target} exists; pass --force to overwrite it")
    if seed is None:
        seed = sentence_seed(text)
    out.parent.mkdir(parents=True, exist_ok=True)
    voice = ResolvedVoice(path=out, worker=worker, command=command, model=model, language=language, device=device,
                          reference=None, reference_text=None, options=dict(options))
    w = Worker(worker_argv(voice, cfg), start_timeout_s=cfg.worker_start_timeout_s,
               sentence_timeout_s=cfg.worker_sentence_timeout_s,
               lock_device=None if device == "cpu" else device, lock_dir=lock_dir(cfg), log=log)
    try:
        w.start(voice.to_hello())
        w.request("design", text=text, language=language, instruct=instruct, seed=seed, out=str(clip))
    finally:
        w.close()
    body: dict = {"command": list(command)} if command is not None else {"worker": worker}
    body.update({"model": model, "language": language, "device": device, "reference": clip.name,
                 "reference_text": text, "options": dict(options)})
    out.write_text(yaml.safe_dump(body, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return load_voice_file(out)
