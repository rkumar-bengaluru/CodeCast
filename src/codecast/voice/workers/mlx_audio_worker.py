"""Voice worker for mlx-audio TTS models on Apple silicon (spec 5.6). Runs in its own environment: no codecast.

UNVERIFIED: written against mlx-audio's documented TTS API (`mlx_audio.tts.utils.load_model`, `model.generate`
yielding results with `.audio` and `.sample_rate`); it has not been run (it needs a Mac).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import _protocol  # noqa: E402

_protocol.setup_streams()

import random  # noqa: E402
from importlib import metadata  # noqa: E402

import mlx.core as mx  # noqa: E402
import numpy as np  # noqa: E402
import soundfile as sf  # noqa: E402
from mlx_audio.tts.utils import load_model  # noqa: E402


def _version(package: str) -> str | None:
    try:
        return metadata.version(package)
    except metadata.PackageNotFoundError:
        return None


class Handler:
    def hello(self, voice: dict) -> dict:
        self.options = dict(voice.get("options") or {})
        self.language = voice.get("language")
        self.reference = voice.get("reference")
        self.reference_text = voice.get("reference_text")
        model_id = voice.get("model") or "mlx-community/OmniVoice-bf16"
        _protocol.progress(f"loading {model_id}")
        self.model = load_model(model_id)
        return {"worker": "mlx-audio", "model": model_id, "device": "gpu",
                "versions": {"mlx-audio": _version("mlx-audio"), "mlx": _version("mlx")}}

    def _seed(self, seed: int) -> None:
        mx.random.seed(seed)
        random.seed(seed)
        np.random.seed(seed % 2**32)

    def _generate(self, text: str, seed: int, out: str, **extra) -> None:
        self._seed(seed)
        kwargs = {**self.options, **extra}
        if self.language and "lang_code" not in kwargs:
            kwargs["lang_code"] = self.language
        chunks, rate = [], 24000 
        for result in self.model.generate(text=text, **kwargs):
            chunks.append(np.array(result.audio.astype(mx.float32)).reshape(-1))
            rate = int(getattr(result, "sample_rate", None) or self.model.sample_rate)
        if not chunks:
            raise RuntimeError("the model produced no audio")
        sf.write(out, np.concatenate(chunks), rate)

    def synthesize(self, text: str, seed: int, out: str) -> None:
        extra = {}
        if self.reference:
            extra["ref_audio"] = str(self.reference)
            if self.reference_text:
                extra["ref_text"] = self.reference_text
        self._generate(text, seed, out, **extra)

    def design(self, text: str, language: str | None, instruct: str | None, seed: int, out: str) -> None:
        extra = {"instruct": instruct} if instruct else {}
        if language:
            extra["lang_code"] = language
        self._generate(text, seed, out, **extra)


if __name__ == "__main__":
    _protocol.serve(Handler())
