"""The pipeline closes the engine it created (also when the run fails or is interrupted), never one passed in."""

from codecast import pipeline
from codecast.config import load_config
from codecast.llm.fake import FakeProvider
from codecast.pipeline import RunOptions, run

from tests.fakes import SilenceEngine


def options(tmp_path):
    return RunOptions(repo="git@github.com:o/r.git", out_dir=tmp_path / "o", workspace=tmp_path / "ws", run_id="r1")


def test_pipeline_closes_engine_it_created_not_given(tmp_path, monkeypatch):
    given = SilenceEngine()
    run(options(tmp_path), load_config(), provider=FakeProvider([]), engine=given, preflight=False)
    assert given.closed == 0
    made = SilenceEngine()
    monkeypatch.setattr(pipeline, "select_engine", lambda *a, **k: made)
    rep = run(options(tmp_path), load_config(), provider=FakeProvider([]), preflight=False)
    assert rep.status != "ok"  # the bad repo fails at ingest; the engine is still closed
    assert made.closed == 1


def test_pipeline_closes_created_engine_on_interrupt(tmp_path, monkeypatch):
    made = SilenceEngine()
    monkeypatch.setattr(pipeline, "select_engine", lambda *a, **k: made)

    def boom(*a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr(pipeline, "ingest", boom)
    run(options(tmp_path), load_config(), provider=FakeProvider([]), preflight=False)
    assert made.closed == 1
