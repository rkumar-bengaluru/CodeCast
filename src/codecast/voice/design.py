"""Designing a voice (spec 5.8): ask a worker to invent a voice, keep the clip and write the whole voice file."""
from __future__ import annotations

import os
from pathlib import Path

import yaml

from codecast.config import VoiceConfig
from codecast.errors import ConfigError
from codecast.voice.model_engine import interpreter_problem, new_worker
from codecast.voice.voicefile import ResolvedVoice, load_voice_file, sentence_seed


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
    voice = ResolvedVoice(path=out, worker=worker, command=command, model=model, language=language, device=device,
                          reference=None, reference_text=None, options=dict(options))
    problem = interpreter_problem(voice, cfg)
    if problem:
        raise ConfigError(problem)
    out.parent.mkdir(parents=True, exist_ok=True)
    tag = os.getpid()
    tmp_clip, tmp_yaml = clip.with_name(f"{clip.stem}.design-{tag}.wav"), out.with_name(f"{out.name}.design-{tag}")
    w = new_worker(voice, cfg)
    try:
        w.start(voice.to_hello())
        w.request("design", text=text, language=language, instruct=instruct, seed=seed, out=str(tmp_clip.resolve()))
        body: dict = {"command": list(command)} if command is not None else {"worker": worker}
        body.update({"model": model, "language": language, "device": device, "reference": clip.name,
                     "reference_text": text, "options": dict(options)})
        tmp_yaml.write_text(yaml.safe_dump(body, allow_unicode=True, sort_keys=False), encoding="utf-8")
        os.replace(tmp_clip, clip)
        os.replace(tmp_yaml, out)
    finally:
        w.close()
        tmp_clip.unlink(missing_ok=True)
        tmp_yaml.unlink(missing_ok=True)
    return load_voice_file(out)
