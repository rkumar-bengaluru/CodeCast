import json

import pytest

from codecast.config import load_config
from codecast.errors import ConfigError
from codecast.estimate import estimate_length, format_estimate
from codecast.models import Storyboard, Submission

from tests.fakes import SilenceEngine
from tests.unit.storyboards import valid_submission


def write_storyboard(ws):
    sub = Submission.model_validate(valid_submission())
    sb = Storyboard(repo_url="u", commit_sha="c", title=sub.title, sources={s.id: s for s in sub.sources},
                    scenes=sub.scenes)
    ws.mkdir(parents=True)
    (ws / "storyboard.json").write_text(sb.model_dump_json(), encoding="utf-8")
    return sb


def test_estimate_uses_storyboard_and_never_the_llm(tmp_path, monkeypatch):
    sb = write_storyboard(tmp_path / "run1")
    monkeypatch.setattr("codecast.llm.registry.create_provider", lambda *a, **k: pytest.fail("LLM called"))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    est = estimate_length(tmp_path / "run1", load_config(env={}), engine=SilenceEngine(0.4))
    assert est.run_id == "run1" and (est.engine, est.voice) == ("silence", "silence")
    assert len(est.per_scene_seconds) == len(sb.scenes) and est.scene_types[0] == "title"
    assert est.spoken_words == 130 and est.total_s == pytest.approx(sum(est.per_scene_seconds), abs=0.05)
    assert est.limits == (50.0, 300.0) and est.within_limits
    saved = json.loads((tmp_path / "run1" / "length_estimate.json").read_text(encoding="utf-8"))
    assert saved["total_s"] == est.total_s


def test_estimate_reports_outside_limits(tmp_path):
    write_storyboard(tmp_path / "run1")
    est = estimate_length(tmp_path / "run1", load_config(env={"CODECAST_DURATION_MIN_S": "0",
                                                              "CODECAST_DURATION_MAX_S": "30"}),
                          engine=SilenceEngine(0.4))
    assert not est.within_limits and est.limits == (0.0, 30.0)
    assert "outside the 0–30 s limit" in format_estimate(est)


def test_estimate_without_storyboard_explains(tmp_path):
    with pytest.raises(ConfigError, match="measures the script of an existing run"):
        estimate_length(tmp_path, load_config(env={}), engine=SilenceEngine())


def test_format_lists_scenes_and_verdict(tmp_path):
    write_storyboard(tmp_path / "run1")
    est = estimate_length(tmp_path / "run1", load_config(env={}), engine=SilenceEngine(0.4))
    text = format_estimate(est)
    assert text.splitlines()[0] == "video length for run run1 (silence voice silence):"
    assert "  scene 1  title" in text
    assert f"total {est.total_s:.1f} s, 130 spoken words: within the 50–300 s limit" in text
