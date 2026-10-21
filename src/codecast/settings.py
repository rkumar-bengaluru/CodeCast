"""Environment overrides: a `.env` file plus the CODECAST_* variables that override config values."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path

from codecast.errors import ConfigError

# config key -> (environment variable, type)
LIMIT_VARS: dict[str, tuple[str, type]] = {
    "duration_min_s": ("CODECAST_DURATION_MIN_S", float),
    "duration_max_s": ("CODECAST_DURATION_MAX_S", float),
    "min_words": ("CODECAST_MIN_WORDS", int),
    "max_words": ("CODECAST_MAX_WORDS", int),
    "target_words": ("CODECAST_TARGET_WORDS", int),
    "min_scenes": ("CODECAST_MIN_SCENES", int),
    "max_scenes": ("CODECAST_MAX_SCENES", int),
}
VOICE_MODEL_VAR = "CODECAST_VOICE_MODEL"

_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")


_QUOTED = re.compile(r"""^(['"])(.*?)\1(?:\s+#.*)?$""")


def _value(raw: str) -> str:
    # Quotes are stripped but nothing inside them is unescaped: Windows paths such as
    # "C:\2026\rkumar\..." must survive as written (python-dotenv would turn "\r" into a carriage return).
    m = _QUOTED.match(raw)
    if m:
        return m.group(2)
    return re.sub(r"\s+#.*$", "", raw)


def load_env_file(path: Path = Path(".env")) -> bool:
    """Load KEY=VALUE lines into os.environ without overriding variables that are already set."""
    path = Path(path)
    if not path.is_file():
        return False
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        raise ConfigError(f"{path} is not UTF-8 text; save it as UTF-8 (PowerShell 5 `>` writes UTF-16)") from None
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = _LINE.match(line)
        if m:
            os.environ.setdefault(m.group(1), _value(m.group(2)))
    return True


def limit_overrides(env: Mapping[str, str] | None = None) -> dict[str, float | int]:
    env = os.environ if env is None else env
    out: dict[str, float | int] = {}
    for key, (var, kind) in LIMIT_VARS.items():
        raw = env.get(var)
        if raw is None or raw.strip() == "":
            continue
        try:
            out[key] = kind(raw.strip())
        except ValueError:
            what = "a whole number" if kind is int else "a number"
            raise ConfigError(f"{var}={raw!r} is not {what}") from None
    return out


def voice_model_from_env(env: Mapping[str, str] | None = None) -> str | None:
    env = os.environ if env is None else env
    return env.get(VOICE_MODEL_VAR) or None
