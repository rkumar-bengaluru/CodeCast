"""Configuration: packaged defaults.yaml deep-merged with an optional user file."""

from __future__ import annotations

from importlib import resources
from pathlib import Path
from collections.abc import Mapping
from typing import Any

import yaml
from pydantic import BaseModel, ValidationError, model_validator

from codecast.errors import ConfigError
from codecast.settings import limit_overrides, voice_model_from_env

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

    @model_validator(mode="after")
    def _ordered(self) -> "LimitsConfig":
        if not 0 <= self.duration_min_s <= self.duration_max_s:
            raise ValueError(f"need 0 <= duration_min_s ({self.duration_min_s:g}) <= duration_max_s "
                             f"({self.duration_max_s:g})")
        if not 0 <= self.min_words <= self.target_words <= self.max_words:
            raise ValueError(f"need 0 <= min_words ({self.min_words}) <= target_words ({self.target_words}) "
                             f"<= max_words ({self.max_words})")
        return self

    @property
    def duration_range(self) -> tuple[float, float]:
        return (self.duration_min_s, self.duration_max_s)


class VoiceConfig(BaseModel):
    model_path: str | None = None  # a custom piper .onnx; overrides piper_voice + voices_dir when set
    piper_voice: str = "en_US-lessac-medium"
    voices_dir: str = "~/.cache/codecast/voices"
    say_voice: str = "Samantha"
    edge_voice: str = "en-US-AriaNeural"
    gap_s: float = 0.15
    tail_s: float = 0.5

    @property
    def voices_path(self) -> Path:
        return Path(self.voices_dir).expanduser()


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
    voice_model = voice_model_from_env(env)
    if voice_model:
        data["voice"]["model_path"] = voice_model
    try:
        LimitsConfig.model_validate(data["limits"])
    except ValidationError as e:
        msg = "; ".join(err["msg"].removeprefix("Value error, ") for err in e.errors())
        raise ConfigError(f"invalid limits: {msg} (from limits: in the config file or the CODECAST_* "
                          f"variables in the environment / .env)") from e
    try:
        return Config.model_validate(data)
    except ValidationError as e:
        raise ConfigError(f"invalid configuration: {e}") from e
