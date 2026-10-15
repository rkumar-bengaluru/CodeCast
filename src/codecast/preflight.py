"""Preflight checks (spec §9.3). Completed in Task 23."""

from codecast.config import Config


def fatal_problems(cfg: Config, *, require_key: bool, allow_network_tts: bool) -> list[str]:
    return []
