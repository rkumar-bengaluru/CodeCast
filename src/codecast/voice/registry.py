"""The engine registry (spec 5.2): engine name -> a kind that reports its status and creates its engine."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from codecast.config import VoiceConfig
    from codecast.voice.engines import TTSEngine

KNOWN_NAMES = ("piper", "say", "edge-tts", "model")  # what `<engine>:<voice>` may name, for the error message


class EngineKind(Protocol):
    name: str

    def status(self, voice_part: str, cfg: VoiceConfig, *, allow_network: bool) -> str:
        """'ok' or the problem, for the voice named after the colon."""
        ...

    def create(self, voice_part: str, cfg: VoiceConfig) -> TTSEngine: ...


_KINDS: dict[str, EngineKind] = {}


def register(kind: EngineKind) -> None:
    _KINDS[kind.name] = kind


def kinds() -> dict[str, EngineKind]:
    return dict(_KINDS)
