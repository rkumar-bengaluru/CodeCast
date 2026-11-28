"""Voice files for the model engine: load, apply platform overrides, and compute the voice_id."""
from __future__ import annotations

import hashlib
import json
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

from codecast.errors import ConfigError

WORKERS_DIR = Path(__file__).parent / "workers"
SHIPPED_WORKERS = {"omnivoice": "omnivoice_worker.py", "mlx-audio": "mlx_audio_worker.py"}
PLATFORMS = ("darwin", "win32", "linux")
OVERRIDABLE = ("worker", "command", "model", "device", "options")


class VoiceFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    worker: str | None = None
    command: list[str] | None = None
    model: str | None = None
    language: str | None = None
    device: str = "auto"
    reference: str | None = None
    reference_text: str | None = None
    options: dict = {}
    platforms: dict[str, dict] = {}


@dataclass(frozen=True)
class ResolvedVoice:
    path: Path
    worker: str | None
    command: list[str] | None
    model: str | None
    language: str | None
    device: str
    reference: Path | None
    reference_text: str | None
    options: dict = field(default_factory=dict)
    voice_id: str = ""

    def to_hello(self) -> dict:
        """The dict sent in the protocol's hello.voice."""
        return {
            "worker": self.worker,
            "model": self.model,
            "language": self.language,
            "device": self.device,
            "reference": str(self.reference) if self.reference else None,
            "reference_text": self.reference_text,
            "options": self.options,
        }


def _nfc(value: Any) -> Any:
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, list):
        return [_nfc(v) for v in value]
    if isinstance(value, dict):
        return {_nfc(k): _nfc(v) for k, v in value.items()}
    return value


def sentence_seed(text: str) -> int:
    """First 8 bytes of SHA-256 over the NFC text, big-endian unsigned."""
    digest = hashlib.sha256(unicodedata.normalize("NFC", text).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


def _lf(data: bytes) -> bytes:
    """CRLF as LF, so a Windows checkout with autocrlf hashes like the wheel and the Mac."""
    return data.replace(b"\r\n", b"\n")


def load_voice_file(path: Path, *, platform: str = sys.platform) -> ResolvedVoice:
    path = Path(path)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as e:
        raise ConfigError(f"voice file {path}: cannot read it ({e})") from e
    except yaml.YAMLError as e:
        raise ConfigError(f"voice file {path}: not valid YAML ({e})") from e
    if not isinstance(raw, dict):
        raise ConfigError(f"voice file {path}: expected a mapping of settings")
    try:
        vf = VoiceFile.model_validate(raw)
    except ValidationError as e:
        raise ConfigError(f"voice file {path}: {e}") from e

    unknown = sorted(set(vf.platforms) - set(PLATFORMS))
    if unknown:
        raise ConfigError(
            f"voice file {path}: unknown platforms key(s) {', '.join(map(str, unknown))}; use {', '.join(PLATFORMS)}"
        )
    fields = vf.model_dump()
    override = vf.platforms.get(platform)
    if override is not None:
        if not isinstance(override, dict):
            raise ConfigError(f"voice file {path}: platforms.{platform} must be a mapping")
        bad = sorted(set(override) - set(OVERRIDABLE))
        if bad:
            raise ConfigError(
                f"voice file {path}: platforms.{platform} may override only {', '.join(OVERRIDABLE)}; not {', '.join(bad)}"
            )
        if "worker" in override and "command" not in override:
            fields["command"] = None
        if "command" in override and "worker" not in override:
            fields["worker"] = None
        fields.update(override)
    try:
        fields = VoiceFile.model_validate({**fields, "platforms": {}}).model_dump()
    except ValidationError as e:
        raise ConfigError(f"voice file {path}: platforms.{platform}: {e}") from e

    worker, command = fields["worker"], fields["command"]
    if (worker is None) == (command is None):
        raise ConfigError(f"voice file {path}: set exactly one of `worker` and `command`")

    folder = path.resolve().parent
    reference = None
    clip = b""
    if fields["reference"]:
        reference = (folder / fields["reference"]).resolve()
        if not reference.is_file():
            raise ConfigError(f"voice file {path}: reference clip {fields['reference']} not found at {reference}")
        clip = reference.read_bytes()

    worker_bytes: list[bytes] = []
    if worker is not None:
        if worker not in SHIPPED_WORKERS:
            raise ConfigError(f"voice file {path}: unknown worker {worker!r}; shipped: {', '.join(SHIPPED_WORKERS)}")
        py = WORKERS_DIR / SHIPPED_WORKERS[worker]
        if not py.is_file():
            raise ConfigError(f"voice file {path}: worker file {py.name} is missing from {WORKERS_DIR}")
        worker_bytes.append(_lf(py.read_bytes()))
        req = WORKERS_DIR / f"{worker}.requirements.txt"
        if req.is_file():
            worker_bytes.append(_lf(req.read_bytes()))

    fields = _nfc(fields)
    worker, command = _nfc(worker), _nfc(command)
    try:
        canonical = json.dumps(
            {
                "worker": worker,
                "command": command,
                "model": fields["model"],
                "language": fields["language"],
                "device": fields["device"],
                "options": fields["options"],
                "reference_text": fields["reference_text"],
            },
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )
    except TypeError as e:
        raise ConfigError(f"voice file {path}: options must be JSON values ({e})") from e
    h = hashlib.sha256()
    for part in (canonical.encode("utf-8"), clip, *worker_bytes):
        h.update(part)
        h.update(b"\x00")
    voice_id = f"{path.stem}-{h.hexdigest()[:12]}"

    if command is not None:
        command = [c.replace("{voice_dir}", str(folder)) for c in command]
    return ResolvedVoice(
        path=path,
        worker=worker,
        command=command,
        model=fields["model"],
        language=fields["language"],
        device=fields["device"],
        reference=reference,
        reference_text=fields["reference_text"],
        options=fields["options"],
        voice_id=voice_id,
    )
