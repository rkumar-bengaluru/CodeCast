"""The real mlx-audio worker on Apple silicon (by hand, on a Mac: `pytest -m gpu tests/gpu`; needs
`codecast voice-env mlx-audio`). Skipped elsewhere."""
from __future__ import annotations

import sys
import time
import wave

import pytest
import yaml

from codecast.config import VoiceConfig
from codecast.voice.model_engine import ModelEngine, interpreter_for
from codecast.voice.voicefile import load_voice_file, sentence_seed

pytestmark = [pytest.mark.gpu, pytest.mark.skipif(sys.platform != "darwin", reason="mlx-audio runs only on macOS")]

MODEL = "mlx-community/OmniVoice-bf16"
REF_TEXT = "नमस्ते, मैं इस श्रृंखला में आपका मार्गदर्शन करूँगी। चलिए शुरू करते हैं।"
SENTENCE = "अब एक पीडीएफ़ चुनिए और अपलोड पर क्लिक कीजिए।"


def _frames(path) -> tuple[int, int]:
    with wave.open(str(path), "rb") as f:
        return f.getnframes(), f.getframerate()


def _samples(path) -> bytes:
    with wave.open(str(path), "rb") as f:
        return f.readframes(f.getnframes())


def _engine(tmp_path, name: str, **fields) -> ModelEngine:
    base = {"worker": "mlx-audio", "model": MODEL, "language": "hi", "device": "auto", **fields}
    (tmp_path / f"{name}.yaml").write_text(yaml.safe_dump(base, allow_unicode=True), encoding="utf-8")
    return ModelEngine(load_voice_file(tmp_path / f"{name}.yaml"), VoiceConfig())


def test_mlx_audio_designs_then_voices_the_same_samples(tmp_path):
    if not interpreter_for("mlx-audio", VoiceConfig()).is_file():
        pytest.skip("run `codecast voice-env mlx-audio` first")
    ref = tmp_path / "ref.wav"
    eng = _engine(tmp_path, "design")
    try:
        t = time.perf_counter()
        eng._started().request("design", text=REF_TEXT, language="hi", instruct="female, low pitch, indian accent",
                               seed=42, out=str(ref))
        print(f"\nload + design: {time.perf_counter() - t:.1f}s; ready: {eng.ready}")
    finally:
        eng.close()
    frames, rate = _frames(ref)
    assert ref.is_file() and frames > rate

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
        print(f"sentence: {first:.1f}s then {time.perf_counter() - t:.1f}s")
    finally:
        eng.close()
    assert _frames(a) == _frames(b) and _frames(a)[0] > _frames(a)[1] // 2
    assert _samples(a) == _samples(b), "the same seed must give identical samples"
