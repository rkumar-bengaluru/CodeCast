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


def test_pipeline_closes_created_engine_before_render(tmp_path, tiny_repo, monkeypatch):
    """The engine is closed once voicing is done (render and compose run without it), and only once."""
    from tests.unit.test_model_engine import storyboard
    ws = tmp_path / "ws" / "r1"
    ws.mkdir(parents=True)
    (ws / "repomap.json").write_text(tiny_repo.model_dump_json(), encoding="utf-8")
    (ws / "storyboard.json").write_text(storyboard().model_dump_json(), encoding="utf-8")
    made = SilenceEngine()
    monkeypatch.setattr(pipeline, "select_engine", lambda *a, **k: made)
    seen: list[int] = []

    class StopRenderer:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def render_scene(self, *a, **k):
            seen.append(made.closed)
            raise RuntimeError("stop at render")

    rep = run(RunOptions(repo="https://github.com/o/tiny", out_dir=tmp_path / "o", workspace=tmp_path / "ws",
                         run_id="r1", from_stage="voice"), load_config(env={"CODECAST_DURATION_MIN_S": "1"}),
              renderer_factory=StopRenderer, preflight=False)
    assert rep.failure is not None and rep.failure.stage == "render", rep.failure
    assert made.calls and seen == [1] and made.closed == 1
