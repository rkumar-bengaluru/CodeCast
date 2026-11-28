"""Voice worker for OmniVoice (k2-fsa/OmniVoice) on PyTorch (spec 5.6). Runs in its own environment: no codecast."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import _protocol  # noqa: E402

_protocol.setup_streams()

import random  # noqa: E402
from importlib import metadata  # noqa: E402

import numpy as np  # noqa: E402
import soundfile as sf  # noqa: E402
import torch  # noqa: E402
from omnivoice import OmniVoice  # noqa: E402

RATE = 24000


def _device(name: str) -> str:
    if name == "auto":
        if torch.cuda.is_available():
            return "cuda"
        mps = getattr(torch.backends, "mps", None)
        return "mps" if mps is not None and mps.is_available() else "cpu"
    return name


def _version(package: str) -> str | None:
    try:
        return metadata.version(package)
    except metadata.PackageNotFoundError:
        return None


class Handler:
    def hello(self, voice: dict) -> dict:
        self.device = _device(voice.get("device") or "auto")
        self.options = dict(voice.get("options") or {})
        self.language = voice.get("language")
        if voice.get("reference") and not voice.get("reference_text"):
            raise ValueError("reference_text is required with reference")
        model_id = voice.get("model") or "k2-fsa/OmniVoice"
        dtype = torch.float16 if self.device.startswith("cuda") else torch.float32
        _protocol.progress(f"loading {model_id} on {self.device}")
        self.model = OmniVoice.from_pretrained(model_id, device_map=self.device, dtype=dtype)
        self.rate = int(getattr(self.model, "sampling_rate", None) or RATE)
        self.prompt = None
        if voice.get("reference"):
            self.prompt = self.model.create_voice_clone_prompt(ref_audio=str(voice["reference"]),
                                                               ref_text=voice.get("reference_text"))
        return {"worker": "omnivoice", "model": model_id, "device": self.device,
                "versions": {"omnivoice": _version("omnivoice"), "torch": torch.__version__}}

    def _seed(self, seed: int) -> None:
        torch.manual_seed(seed)
        random.seed(seed)
        np.random.seed(seed % 2**32)  # numpy's legacy seeding takes less than 2**32
        if self.device.startswith("cuda"):
            torch.cuda.manual_seed_all(seed)

    def _write(self, audio, out: str) -> None:
        if hasattr(audio, "detach"):
            audio = audio.detach().float().cpu().numpy()
        sf.write(out, np.asarray(audio, dtype=np.float32).reshape(-1), self.rate)

    def synthesize(self, text: str, seed: int, out: str) -> None:
        self._seed(seed)
        kwargs = dict(self.options)
        if self.language and "language" not in kwargs:
            kwargs["language"] = self.language
        if self.prompt is not None:
            kwargs["voice_clone_prompt"] = self.prompt
        self._write(self.model.generate(text=text, **kwargs)[0], out)

    def design(self, text: str, language: str | None, instruct: str | None, seed: int, out: str) -> None:
        self._seed(seed)
        kwargs = {k: v for k, v in self.options.items() if k != "instruct"}
        if instruct:
            kwargs["instruct"] = instruct
        language = language or self.language
        if language and "language" not in kwargs:
            kwargs["language"] = language
        self._write(self.model.generate(text=text, **kwargs)[0], out)


if __name__ == "__main__":
    _protocol.serve(Handler())
