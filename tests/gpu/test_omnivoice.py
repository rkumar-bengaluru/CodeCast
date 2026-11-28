"""The real OmniVoice worker on the GPU (by hand: `pytest -m gpu tests/gpu`; needs `codecast voice-env omnivoice`)."""
from __future__ import annotations

import subprocess
import time
import wave

import pytest
import yaml

from codecast.config import VoiceConfig
from codecast.voice.model_engine import ModelEngine, interpreter_for
from codecast.voice.voicefile import load_voice_file, sentence_seed

pytestmark = pytest.mark.gpu

REF_TEXT = "नमस्ते, मैं इस श्रृंखला में आपका मार्गदर्शन करूँगी। चलिए शुरू करते हैं।"
SENTENCE = "अब एक पीडीएफ़ चुनिए और अपलोड पर क्लिक कीजिए।"
RATE = 24000


def _gpu_used_gib() -> float | None:
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, check=True).stdout
        return float(out.splitlines()[0]) / 1024
    except (OSError, subprocess.CalledProcessError, ValueError, IndexError):
        return None


def _frames(path) -> tuple[int, int]:
    with wave.open(str(path), "rb") as f:
        return f.getnframes(), f.getframerate()


def _samples(path) -> bytes:
    with wave.open(str(path), "rb") as f:
        return f.readframes(f.getnframes())


def _engine(tmp_path, name: str, **fields) -> ModelEngine:
    base = {"worker": "omnivoice", "model": "k2-fsa/OmniVoice", "language": "hi", "device": "cuda", **fields}
    (tmp_path / f"{name}.yaml").write_text(yaml.safe_dump(base, allow_unicode=True), encoding="utf-8")
    return ModelEngine(load_voice_file(tmp_path / f"{name}.yaml"), VoiceConfig())


def test_omnivoice_voices_and_designs(tmp_path):
    if not interpreter_for("omnivoice", VoiceConfig()).is_file():
        pytest.skip("run `codecast voice-env omnivoice` first")
    ref = tmp_path / "ref.wav"
    eng = _engine(tmp_path, "design")
    try:
        t = time.perf_counter()
        eng._started().request("design", text=REF_TEXT, language="hi", instruct="female, low pitch, indian accent",
                               seed=42, out=str(ref))
        print(f"\nload + design: {time.perf_counter() - t:.1f}s; ready: {eng.ready}")
    finally:
        eng.close()
    assert ref.is_file() and _frames(ref)[1] == RATE and _frames(ref)[0] > RATE

    eng = _engine(tmp_path, "voice", reference=str(ref), reference_text=REF_TEXT)
    a, b = tmp_path / "a.wav", tmp_path / "b.wav"
    seed = sentence_seed(SENTENCE)
    try:
        w = eng._started()
        t = time.perf_counter()
        w.request("synthesize", text=SENTENCE, seed=seed, out=str(a))
        first = time.perf_counter() - t
        t = time.perf_counter()
        w.request("synthesize", text=SENTENCE, seed=seed, out=str(b))
        second = time.perf_counter() - t
        used = _gpu_used_gib()
        print(f"sentence: {first:.1f}s then {second:.1f}s; GPU memory in use {used} GiB")
    finally:
        eng.close()
    assert _frames(a) == _frames(b) and _frames(a)[0] > RATE // 2 and _frames(a)[1] == RATE
    assert _samples(a) == _samples(b), "the same seed must give identical samples"
    if used is not None:
        assert used < 6, f"GPU memory in use {used:.2f} GiB"
