"""Local-first TTS engines (spec §8): piper → say → edge-tts (network, opt-in). One engine per video."""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
import tempfile
import wave
from pathlib import Path
from typing import Protocol

from codecast.config import VoiceConfig, resolved_voice, winning_setting
from codecast.errors import CodecastError, ConfigError
from codecast.voice import registry


class TTSEngine(Protocol):
    name: str
    voice_id: str  # which voice within the engine; part of the audio cache key

    def synthesize(self, text: str, out_wav: Path) -> None: ...

    def close(self) -> None: ...


def convert_to_wav48k(src: Path, dst: Path) -> None:
    try:
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-ac", "1", "-ar", "48000",
                        "-c:a", "pcm_s16le", str(dst)], check=True, capture_output=True, timeout=120)
    except subprocess.CalledProcessError as e:
        raise CodecastError(f"audio conversion failed: {e.stderr.decode(errors='replace')[-300:]}", stage="voice") from e


def wav_duration(path: Path) -> float:
    with wave.open(str(path)) as w:
        return w.getnframes() / w.getframerate()


class SayEngine:
    name = "say"

    def __init__(self, voice: str = "Samantha"):
        self.voice = self.voice_id = voice

    def synthesize(self, text: str, out_wav: Path) -> None:
        # macOS `say` treats `<word>` as embedded markup and silently drops the rest of the text,
        # so angle brackets are spoken as words before the text is handed over.
        spoken = text.replace("<", " less than ").replace(">", " greater than ")
        with tempfile.TemporaryDirectory() as td:
            txt, aiff = Path(td) / "in.txt", Path(td) / "out.aiff"
            txt.write_text(spoken, encoding="utf-8")
            subprocess.run(["say", "-v", self.voice, "-o", str(aiff), "-f", str(txt)],
                           check=True, capture_output=True, timeout=120)
            convert_to_wav48k(aiff, out_wav)

    def close(self) -> None:
        pass


class PiperEngine:
    name = "piper"

    def __init__(self, model_path: Path):
        from piper import PiperVoice
        self.voice_id = Path(model_path).stem
        self._voice = PiperVoice.load(str(model_path))

    def synthesize(self, text: str, out_wav: Path) -> None:
        with tempfile.TemporaryDirectory() as td:
            raw = Path(td) / "raw.wav"
            with wave.open(str(raw), "wb") as wf:
                synth = getattr(self._voice, "synthesize_wav", None) or self._voice.synthesize
                synth(text, wf)
            convert_to_wav48k(raw, out_wav)

    def close(self) -> None:
        pass


class EdgeEngine:
    name = "edge-tts"

    def __init__(self, voice: str = "en-US-AriaNeural"):
        self.voice = self.voice_id = voice

    def synthesize(self, text: str, out_wav: Path) -> None:
        with tempfile.TemporaryDirectory() as td:
            txt, mp3 = Path(td) / "in.txt", Path(td) / "out.mp3"
            txt.write_text(text, encoding="utf-8")
            subprocess.run([sys.executable, "-m", "edge_tts", "--voice", self.voice, "--file", str(txt),
                            "--write-media", str(mp3)], check=True, capture_output=True, timeout=120)
            convert_to_wav48k(mp3, out_wav)

    def close(self) -> None:
        pass


def _piper_installed() -> bool:
    return importlib.util.find_spec("piper") is not None


def _edge_installed() -> bool:
    return importlib.util.find_spec("edge_tts") is not None


def _say_available(voice: str) -> bool:
    if shutil.which("say") is None:
        return False
    out = subprocess.run(["say", "-v", "?"], capture_output=True, text=True, timeout=30).stdout
    return any(line.split()[0] == voice for line in out.splitlines() if line.strip())


def piper_model_path(cfg: VoiceConfig) -> Path:
    if cfg.model_path:
        return Path(cfg.model_path).expanduser()
    return cfg.voices_path / f"{cfg.piper_voice}.onnx"


def _custom_voice_problem(cfg: VoiceConfig, model: Path | None = None) -> str | None:
    model = model or piper_model_path(cfg)
    if not model.is_file():
        return f"custom voice not found: {model}"
    sidecar = model.with_name(model.name + ".json")
    if not sidecar.is_file():
        return f"custom voice config missing: {sidecar} (piper needs it next to the .onnx)"
    return None


def engine_status(cfg: VoiceConfig, *, allow_network: bool) -> dict[str, str]:
    status = {}
    if not _piper_installed():
        status["piper"] = "piper-tts not installed"
    elif cfg.model_path:
        status["piper"] = _custom_voice_problem(cfg) or "ok"
    elif not piper_model_path(cfg).is_file():
        status["piper"] = "voice missing; run `codecast fetch-voice`"
    else:
        status["piper"] = "ok"
    status["say"] = "ok" if _say_available(cfg.say_voice) else f"macOS say or voice {cfg.say_voice!r} unavailable"
    if not _edge_installed():
        status["edge-tts"] = "edge-tts not installed"
    else:
        status["edge-tts"] = "ok" if allow_network else "disabled (needs --allow-network-tts)"
    model = registry.kinds().get("model")
    status["model"] = model.status("", cfg, allow_network=allow_network) if model else "model engine not available"
    return status


class _PiperKind:
    name = "piper"

    @staticmethod
    def _path(voice_part: str, cfg: VoiceConfig) -> Path:
        if "/" in voice_part or "\\" in voice_part or voice_part.endswith(".onnx"):
            return Path(voice_part).expanduser()
        return cfg.voices_path / f"{voice_part}.onnx"

    def status(self, voice_part: str, cfg: VoiceConfig, *, allow_network: bool) -> str:
        if not _piper_installed():
            return "piper-tts not installed"
        model = self._path(voice_part, cfg)
        if model == Path(voice_part).expanduser():
            return _custom_voice_problem(cfg, model) or "ok"
        if not model.is_file():  # a voice under voices_dir, as today's default: no sidecar check
            return f"voice {voice_part!r} not found in {cfg.voices_path}; run `codecast fetch-voice` or give a path"
        return "ok"

    def create(self, voice_part: str, cfg: VoiceConfig) -> TTSEngine:
        return PiperEngine(self._path(voice_part, cfg))


class _SayKind:
    name = "say"

    def status(self, voice_part: str, cfg: VoiceConfig, *, allow_network: bool) -> str:
        return "ok" if _say_available(voice_part) else f"macOS say or voice {voice_part!r} unavailable"

    def create(self, voice_part: str, cfg: VoiceConfig) -> TTSEngine:
        return SayEngine(voice_part)


class _EdgeKind:
    name = "edge-tts"

    def status(self, voice_part: str, cfg: VoiceConfig, *, allow_network: bool) -> str:
        if not _edge_installed():
            return "edge-tts not installed"
        return "ok" if allow_network else "disabled (needs --allow-network-tts)"

    def create(self, voice_part: str, cfg: VoiceConfig) -> TTSEngine:
        return EdgeEngine(voice_part)


for _kind in (_PiperKind(), _SayKind(), _EdgeKind()):
    registry.register(_kind)

from codecast.voice import model_engine as _model_engine  # noqa: E402,F401 - registers the `model` kind


def named_kind(voice: str) -> tuple[registry.EngineKind | None, str, str]:
    """Split `<engine>:<voice>` -> (kind or None if unregistered, engine name, voice part)."""
    engine, _, part = voice.partition(":")
    return registry.kinds().get(engine), engine, part


def named_problem(voice: str, cfg: VoiceConfig, *, allow_network: bool) -> str | None:
    """None when the named voice can be made; else why not (never a fallback)."""
    kind, engine, part = named_kind(voice)
    if kind is None:
        if engine in registry.KNOWN_NAMES:
            return f"{engine} engine not available"
        return f"unknown engine {engine!r}: choose one of {', '.join(registry.KNOWN_NAMES)}"
    st = kind.status(part, cfg, allow_network=allow_network)
    return None if st == "ok" else f"{engine}:{part}: {st}"


def select_engine(cfg: VoiceConfig, *, allow_network: bool) -> TTSEngine:
    named = resolved_voice(cfg)
    if named is not None:
        if winning_setting(cfg) == "model":
            # --voice-model / CODECAST_VOICE_MODEL / config model_path: today's messages word for word (R3)
            problem = registry.kinds()["piper"].status(cfg.model_path, cfg, allow_network=allow_network)
            if problem != "ok":
                raise ConfigError(f"{problem} (from --voice-model or CODECAST_VOICE_MODEL)")
        elif problem := named_problem(named, cfg, allow_network=allow_network):
            raise ConfigError(problem)
        kind, _, part = named_kind(named)
        return kind.create(part, cfg)
    st = engine_status(cfg, allow_network=allow_network)
    if cfg.model_path and st["piper"] != "ok":
        # An explicitly chosen voice never silently falls back to another one.
        raise ConfigError(f"{st['piper']} (from --voice-model or CODECAST_VOICE_MODEL)")
    if st["piper"] == "ok":
        return PiperEngine(piper_model_path(cfg))
    if st["say"] == "ok":
        return SayEngine(cfg.say_voice)
    if st["edge-tts"] == "ok":
        return EdgeEngine(cfg.edge_voice)
    raise ConfigError("no TTS engine available: run `codecast fetch-voice` (piper), use macOS `say`, "
                      f"or pass --allow-network-tts with edge-tts installed. Status: {st}")
