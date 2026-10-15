import json
import shutil
import subprocess
from pathlib import Path

import pytest

from codecast.config import load_config
from codecast.llm.fake import FakeProvider, tool_call
from codecast.pipeline import RunOptions, run

from tests.fakes import SilenceEngine
from tests.unit.storyboards import valid_submission

pytestmark = pytest.mark.integration


@pytest.fixture
def git_repo(tiny_repo, tmp_path):
    src = Path(tiny_repo.root)
    for args in (["init", "-q", "-b", "main"], ["add", "."], ["commit", "-q", "-m", "init"],
                 ["remote", "add", "origin", "https://github.com/o/tiny"]):
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=src, check=True,
                       capture_output=True)
    return src


def opts(git_repo, tmp_path, **kw):
    return RunOptions(repo=str(git_repo), out_dir=tmp_path / "output", workspace=tmp_path / "ws", run_id="r1", **kw)


def submit():
    return tool_call("submit_storyboard", {"storyboard": valid_submission()})


def test_happy_path_produces_all_artifacts(git_repo, tmp_path):
    # 130 words * 0.4 s = 52 s speech + 2 gaps + 6 tails ≈ 55.4 s → inside [50, 60]
    rep = run(opts(git_repo, tmp_path), load_config(), provider=FakeProvider([submit()]),
              engine=SilenceEngine(0.4), preflight=False)
    assert rep.status == "ok", rep.failure
    out = tmp_path / "output" / "tiny"
    for name in ("explainer.mp4", "explainer.srt", "storyboard.json", "run_report.json"):
        assert (out / name).is_file()
    assert 50 <= rep.video.duration_s <= 60
    assert rep.validation.storyboard_rounds == 1 and rep.tts_engine == "silence"
    assert set(rep.stage_seconds) == {"ingest", "agent", "voice", "render", "compose"}
    assert json.loads((out / "storyboard.json").read_text())["repo_url"] == "https://github.com/o/tiny"
    assert not list(out.glob(".staging-*"))


def test_duration_loop_revises_then_succeeds(git_repo, tmp_path):
    engine = SilenceEngine(0.5)                       # first pass ≈ 69 s → too long
    provider = FakeProvider([submit(), lambda req: _shorter(engine) or submit()])
    rep = run(opts(git_repo, tmp_path), load_config(), provider=provider, engine=engine, preflight=False)
    assert rep.status == "ok", rep.failure
    assert rep.validation.duration_rounds == 1
    assert '"target": [50.0, 60.0]' in provider.requests[1].messages[-1].text()


def _shorter(engine):
    engine.seconds_per_word = 0.4                     # the "revised" narration speaks faster
    return None


def test_duration_failure_exit_3_and_existing_output_untouched(git_repo, tmp_path):
    out = tmp_path / "output" / "tiny"
    out.mkdir(parents=True)
    (out / "explainer.mp4").write_bytes(b"committed demo")
    rep = run(opts(git_repo, tmp_path), load_config(), provider=FakeProvider([submit(), submit(), submit()]),
              engine=SilenceEngine(0.6), preflight=False)
    assert rep.exit_code == 3 and rep.validation.duration_rounds == 2
    assert (out / "explainer.mp4").read_bytes() == b"committed demo"
    assert json.loads((out / "last_failed_run_report.json").read_text())["exit_code"] == 3
    assert (tmp_path / "ws" / "r1" / "run_report.json").is_file()


def test_ingest_failure_exit_2(tmp_path):
    rep = run(RunOptions(repo="git@github.com:o/r.git", out_dir=tmp_path / "o", workspace=tmp_path / "ws", run_id="r2"),
              load_config(), provider=FakeProvider([]), engine=SilenceEngine(), preflight=False)
    assert rep.exit_code == 2 and rep.failure.stage == "ingest"


def test_resume_from_render_reuses_workspace(git_repo, tmp_path):
    first = run(opts(git_repo, tmp_path), load_config(), provider=FakeProvider([submit()]),
                engine=SilenceEngine(0.4), preflight=False)
    assert first.status == "ok"
    shutil.rmtree(tmp_path / "output")
    rep = run(opts(git_repo, tmp_path, from_stage="render"), load_config(), provider=FakeProvider([]),
              engine=SilenceEngine(0.4), preflight=False)
    assert rep.status == "ok", rep.failure
    assert (tmp_path / "output" / "tiny" / "explainer.mp4").is_file()
