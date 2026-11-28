"""Configuration: packaged defaults.yaml deep-merged with an optional user file."""

from __future__ import annotations

from importlib import resources
from pathlib import Path
from collections.abc import Mapping
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, ValidationError, model_validator

from codecast.errors import ConfigError
from codecast.settings import limit_overrides, voice_from_env, voice_model_from_env

KNOWN_PROVIDERS = ("anthropic", "openai")
DEFAULT_KEY_ENV = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}


class LLMConfig(BaseModel):
    provider: str
    model: str | None
    api_key_env: str
    base_url: str | None = None
    temperature: float = 0.3
    max_tokens: int = 4096
    timeout_s: float = 120
    extended_thinking: bool = False
    caps: dict[str, Any] = {}


class AgentConfig(BaseModel):
    max_steps: int | None = None
    max_total_tokens: int = 600_000
    wall_clock_s: float = 240
    revision_steps: int = 15


class LimitsConfig(BaseModel):
    duration_min_s: float = 50.0
    duration_max_s: float = 300.0
    min_words: int = 110
    max_words: int = 200
    target_words: int = 155
    min_scenes: int = 4
    max_scenes: int = 10

    @model_validator(mode="after")
    def _ordered(self) -> "LimitsConfig":
        if not 0 <= self.duration_min_s <= self.duration_max_s:
            raise ValueError(f"need 0 <= duration_min_s ({self.duration_min_s:g}) <= duration_max_s "
                             f"({self.duration_max_s:g})")
        if not 0 <= self.min_words <= self.target_words <= self.max_words:
            raise ValueError(f"need 0 <= min_words ({self.min_words}) <= target_words ({self.target_words}) "
                             f"<= max_words ({self.max_words})")
        if not 2 <= self.min_scenes <= self.max_scenes:  # a title and an outro at least
            raise ValueError(f"need 2 <= min_scenes ({self.min_scenes}) <= max_scenes ({self.max_scenes})")
        return self

    @property
    def duration_range(self) -> tuple[float, float]:
        return (self.duration_min_s, self.duration_max_s)


Source = Literal["flag", "env", "config"]
_STRENGTH = {"flag": 3, "env": 2, "config": 1}


class VoiceConfig(BaseModel):
    voice: str | None = None  # "<engine>:<voice>"; see resolved_voice
    model_path: str | None = None  # a custom piper .onnx; overrides piper_voice + voices_dir when set
    voice_source: Source | None = Field(default=None, exclude=True)
    model_path_source: Source | None = Field(default=None, exclude=True)
    worker_start_timeout_s: float = 1800
    worker_sentence_timeout_s: float = 300
    envs_dir: str = "~/.cache/codecast/envs"
    worker_python: dict[str, str] = {}

    def __setattr__(self, name: str, value: Any) -> None:
        # Assigning voice or model_path after load is the flag layer, and paths are made absolute when stored.
        if name == "voice":
            super().__setattr__("voice_source", "flag" if value else None)
            value = absolute_voice(value) if value else value
        elif name == "model_path":
            super().__setattr__("model_path_source", "flag" if value else None)
            value = _absolute(str(value)) if value else value
        super().__setattr__(name, value)
    piper_voice: str = "en_US-lessac-medium"
    voices_dir: str = "~/.cache/codecast/voices"
    say_voice: str = "Samantha"
    edge_voice: str = "en-US-AriaNeural"
    gap_s: float = 0.15
    tail_s: float = 0.5

    @property
    def voices_path(self) -> Path:
        return Path(self.voices_dir).expanduser()


_VOICE_NAMES = {
    "flag": ("--voice", "--voice-model"),
    "env": ("CODECAST_VOICE", "CODECAST_VOICE_MODEL"),
    "config": ("voice.voice", "voice.model_path"),
}


def _looks_like_path(part: str) -> bool:
    return "/" in part or "\\" in part or part.lower().endswith((".onnx", ".yaml", ".yml"))


def _absolute(part: str) -> str:
    p = Path(part).expanduser()
    return str(p if p.is_absolute() else Path.cwd() / p)


def absolute_voice(voice: str) -> str:
    """Make the path of a path-like `piper:`/`model:` voice absolute (against the cwd now); others unchanged."""
    engine, sep, part = voice.partition(":")
    if sep and engine in ("piper", "model") and part and _looks_like_path(part):
        return f"{engine}:{_absolute(part)}"
    return voice


def resolved_voice(vc: VoiceConfig) -> str | None:
    """The winning `<engine>:<voice>` (spec 5.1), or None when nothing names a voice (the probe picks)."""
    layers = [(src, kind) for kind, src, val in (("voice", vc.voice_source, vc.voice),
                                                 ("model", vc.model_path_source, vc.model_path)) if val and src]
    if not layers:
        return None
    top = max(_STRENGTH[src] for src, _ in layers)
    winners = [kind for src, kind in layers if _STRENGTH[src] == top]
    src = next(s for s, _ in layers if _STRENGTH[s] == top)
    if len(winners) > 1:
        a, b = _VOICE_NAMES[src]
        raise ConfigError(f"{a} and {b} both name a voice; set only one (two choices, neither silently wins)")
    if winners[0] == "model":
        return f"piper:{vc.model_path}"
    engine, sep, part = vc.voice.partition(":")
    if not sep or not engine or not part:
        raise ConfigError(f"voice {vc.voice!r} must be <engine>:<voice>, e.g. say:Samantha")
    return vc.voice


def apply_voice_flags(cfg: Config, *, voice: str | None, voice_model: str | None) -> None:
    """Set the flag layer (--voice, --voice-model), then check the choice (a same-layer conflict is a ConfigError)."""
    if voice:
        cfg.voice.voice = voice
    if voice_model:
        cfg.voice.model_path = str(voice_model)
    resolved_voice(cfg.voice)


class RenderConfig(BaseModel):
    terminal_output_allowlist: list[str]


class ModelRate(BaseModel):
    input_per_mtok: float
    output_per_mtok: float
    cache_read_per_mtok: float
    cache_write_per_mtok: float


class Config(BaseModel):
    llm: LLMConfig
    agent: AgentConfig
    voice: VoiceConfig
    render: RenderConfig
    limits: LimitsConfig = LimitsConfig()
    pricing: dict[str, ModelRate] = {}


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _defaults() -> dict:
    text = resources.files("codecast").joinpath("defaults.yaml").read_text(encoding="utf-8")
    return yaml.safe_load(text)


def load_config(path: Path | None = None, *, env: Mapping[str, str] | None = None) -> Config:
    """Packaged defaults < user YAML file < CODECAST_* environment variables (`env=None` reads os.environ)."""
    data = _defaults()
    user: dict = {}
    if path is not None:
        path = Path(path)
        if not path.is_file():
            raise ConfigError(f"config file not found: {path}")
        try:
            user = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as e:
            raise ConfigError(f"invalid YAML in {path}: {e}") from e
        if not isinstance(user, dict):
            raise ConfigError(f"config root must be a mapping: {path}")
        for section, default_value in data.items():
            if isinstance(default_value, dict) and section in user and not isinstance(user[section], dict):
                raise ConfigError(f"config section {section!r} must be a mapping in {path}")
        data = _deep_merge(data, user)

    user_llm = user.get("llm") or {}
    provider = data["llm"]["provider"]
    if provider not in KNOWN_PROVIDERS:
        raise ConfigError(f"unknown llm.provider {provider!r}; expected one of {KNOWN_PROVIDERS}")
    if provider != "anthropic" and not user_llm.get("model"):
        raise ConfigError(f"llm.model must be set explicitly when llm.provider is {provider!r}")
    if "api_key_env" not in user_llm:
        data["llm"]["api_key_env"] = DEFAULT_KEY_ENV[provider]
    if data["llm"].get("extended_thinking"):
        raise ConfigError("llm.extended_thinking is incompatible with forced tool_choice in Phase A")

    data["limits"] = {**(data.get("limits") or {}), **limit_overrides(env)}
    user_voice = user.get("voice") or {}
    sources: dict[str, str] = {k: "config" for k in ("voice", "model_path") if user_voice.get(k)}
    for key, val in (("voice", voice_from_env(env)), ("model_path", voice_model_from_env(env))):
        if val:
            data["voice"][key] = val
            sources[key] = "env"
    if data["voice"].get("voice"):
        data["voice"]["voice"] = absolute_voice(str(data["voice"]["voice"]))
    if data["voice"].get("model_path"):
        data["voice"]["model_path"] = _absolute(str(data["voice"]["model_path"]))
    try:
        LimitsConfig.model_validate(data["limits"])
    except ValidationError as e:
        msg = "; ".join(err["msg"].removeprefix("Value error, ") for err in e.errors())
        raise ConfigError(f"invalid limits: {msg} (from limits: in the config file or the CODECAST_* "
                          f"variables in the environment / .env)") from e
    try:
        cfg = Config.model_validate(data)
    except ValidationError as e:
        raise ConfigError(f"invalid configuration: {e}") from e
    cfg.voice.voice_source = sources.get("voice")  # after the values: assigning those would mark them "flag"
    cfg.voice.model_path_source = sources.get("model_path")
    return cfg
