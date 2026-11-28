"""Preflight checks (spec §9.3). Shared by `codecast doctor` and the start of `codecast run`."""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from codecast.voice import registry
from codecast.compose.ffmpeg import ffmpeg_problems
from codecast.config import Config, resolved_voice, winning_setting
from codecast.voice.engines import engine_status, named_kind, named_problem, piper_model_path


@dataclass
class Check:
    name: str
    ok: bool
    message: str
    fix: str | None
    fatal: bool


def _chromium_problem() -> str | None:
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            p.chromium.launch().close()
        return None
    except Exception as e:  # noqa: BLE001
        return f"Playwright Chromium cannot start: {str(e).splitlines()[0]}"


def _developer_mode_on() -> bool:
    """Windows Developer Mode (lets the Hugging Face cache link files); a missing key counts as off."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SOFTWARE\Microsoft\Windows\CurrentVersion\AppModelUnlock") as key:
            return winreg.QueryValueEx(key, "AllowDevelopmentWithoutDevLicense")[0] == 1
    except OSError:
        return False


def _model_voice_checks(cfg: Config, named: str, *, deep: bool) -> list[Check]:
    """What a `model:` voice will run, whether it can drift, and (deep) whether its worker starts."""
    from codecast.errors import CodecastError
    from codecast.voice.model_engine import new_worker, worker_argv
    from codecast.voice.voicefile import load_voice_file
    try:
        voice = load_voice_file(Path(named.partition(":")[2]).expanduser())
        argv = worker_argv(voice, cfg.voice)
    except (CodecastError, KeyError):
        return []  # the tts check already reports it
    checks = [Check("voice command", True, "runs: " + " ".join(argv), None, False)]
    if voice.reference is None:
        checks.append(Check("voice reference", False,
                            "the voice has no reference clip, so the model may speak in a different voice each run",
                            "run `codecast voice-design` to make one, or add `reference` and `reference_text`", False))
    if sys.platform == "win32" and not _developer_mode_on():
        checks.append(Check("developer mode", False,
                            "Windows Developer Mode is off: the Hugging Face cache copies files instead of "
                            "linking them (more disk)", "Settings > System > For developers > Developer Mode",
                            False))
    if deep:
        w = new_worker(voice, cfg.voice)
        t0 = time.monotonic()
        try:
            ready = w.start(voice.to_hello())
            checks.append(Check("voice worker", True,
                                f"ready in {time.monotonic() - t0:.1f}s: {json.dumps(ready, ensure_ascii=True)}",
                                None, True))
        except CodecastError as e:
            checks.append(Check("voice worker", False, e.message, "see the worker's messages above", True))
        finally:
            w.close()
    return checks


def run_checks(cfg: Config, *, require_key: bool, allow_network_tts: bool = False, deep: bool = False) -> list[Check]:
    checks = []
    ff = ffmpeg_problems()
    checks.append(Check("ffmpeg", not ff, "; ".join(ff) or "ffmpeg >= 7 with overlay, loudnorm, -/filter_complex",
                        "brew install ffmpeg" if ff else None, True))
    chrome = _chromium_problem()
    checks.append(Check("chromium", chrome is None, chrome or "headless Chromium starts",
                        ".venv/bin/python -m playwright install chromium" if chrome else None, True))
    st = engine_status(cfg.voice, allow_network=allow_network_tts)
    named = resolved_voice(cfg.voice)
    if named is not None and winning_setting(cfg.voice) == "model":
        problem = registry.kinds()["piper"].status(cfg.voice.model_path, cfg.voice, allow_network=allow_network_tts)
        stem = Path(cfg.voice.model_path).stem
        checks.append(Check("tts", problem == "ok", f"engine: piper ({stem})" if problem == "ok" else problem,
                            None if problem == "ok" else "fix the path given by --voice-model or CODECAST_VOICE_MODEL",
                            True))
    elif named is not None:
        problem = named_problem(named, cfg.voice, allow_network=allow_network_tts)
        _, engine, part = named_kind(named)
        shown = Path(part).stem if engine == "piper" else part
        checks.append(Check("tts", problem is None, f"engine: {engine} ({shown})" if problem is None else problem,
                            None if problem is None else "fix the voice named by --voice, CODECAST_VOICE or config",
                            True))
        if named.startswith("model:") and problem is None:
            checks.extend(_model_voice_checks(cfg, named, deep=deep))
    elif cfg.voice.model_path and st["piper"] != "ok":
        checks.append(Check("tts", False, st["piper"], "fix the path given by --voice-model or CODECAST_VOICE_MODEL",
                            True))
    else:
        chosen = next((name for name in ("piper", "say", "edge-tts") if st[name] == "ok"), None)
        voice = f" ({piper_model_path(cfg.voice).stem})" if chosen == "piper" else ""
        checks.append(Check("tts", chosen is not None, f"engine: {chosen}{voice}" if chosen else f"no engine: {st}",
                            None if chosen else "codecast fetch-voice  (or run on macOS with `say`)", True))
    has_key = bool(os.environ.get(cfg.llm.api_key_env))
    checks.append(Check("llm api key", has_key,
                        f"{cfg.llm.api_key_env} is set" if has_key else f"{cfg.llm.api_key_env} is not set",
                        None if has_key else f"export {cfg.llm.api_key_env}=...", require_key))
    return checks


def fatal_problems(cfg: Config, *, require_key: bool, allow_network_tts: bool) -> list[str]:
    return [f"{c.name}: {c.message} (fix: {c.fix})" for c in run_checks(cfg, require_key=require_key,
                                                                       allow_network_tts=allow_network_tts)
            if c.fatal and not c.ok]
