"""--compute-video-length: speak an existing run's storyboard and report its length, without the LLM."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from codecast.config import Config
from codecast.errors import ConfigError
from codecast.models import Storyboard
from codecast.voice.engines import TTSEngine, select_engine
from codecast.voice.timeline import synthesize_storyboard


class LengthEstimate(BaseModel):
    run_id: str
    engine: str
    voice: str
    scene_types: list[str]
    per_scene_seconds: list[float]
    total_s: float
    spoken_words: int
    limits: tuple[float, float]
    within_limits: bool


def estimate_length(ws: Path, cfg: Config, *, engine: TTSEngine | None = None,
                    allow_network: bool = False) -> LengthEstimate:
    """The real length: the same timeline total that the final ffprobe check compares, not a word-rate guess."""
    ws = Path(ws)
    path = ws / "storyboard.json"
    if not path.is_file():
        raise ConfigError(f"no storyboard.json in {ws}; --compute-video-length measures the script of an existing run")
    sb = Storyboard.model_validate_json(path.read_text(encoding="utf-8"))
    engine = engine or select_engine(cfg.voice, allow_network=allow_network)
    # Own cache folder; the per-voice cache key makes a repeat measurement with the same voice instant.
    timeline = synthesize_storyboard(sb, engine, ws / "audio" / "estimate", gap_s=cfg.voice.gap_s,
                                     tail_s=cfg.voice.tail_s)
    lo, hi = cfg.limits.duration_range
    est = LengthEstimate(
        run_id=ws.name, engine=engine.name, voice=getattr(engine, "voice_id", ""),
        scene_types=[sc.type for sc in sb.scenes],
        per_scene_seconds=[round(s, 2) for s in timeline.per_scene_seconds()], total_s=round(timeline.total, 2),
        spoken_words=sum(len(s.text.split()) for sc in sb.scenes for s in sc.narration),
        limits=(lo, hi), within_limits=lo <= timeline.total <= hi)
    (ws / "length_estimate.json").write_text(est.model_dump_json(indent=1), encoding="utf-8")
    return est


def format_estimate(est: LengthEstimate) -> str:
    lo, hi = est.limits
    lines = [f"video length for run {est.run_id} ({est.engine} voice {est.voice}):"]
    lines += [f"  scene {i}  {kind:<14}{secs:6.1f} s"
              for i, (kind, secs) in enumerate(zip(est.scene_types, est.per_scene_seconds), start=1)]
    verdict = "within" if est.within_limits else "outside"
    lines.append(f"  total {est.total_s:.1f} s, {est.spoken_words} spoken words: {verdict} the {lo:g}–{hi:g} s limit")
    return "\n".join(lines)
