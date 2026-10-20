import pytest

from codecast import cli


def test_doctor_exit_codes(monkeypatch, capsys):
    from codecast.preflight import Check
    monkeypatch.setattr(cli, "run_checks", lambda cfg, require_key, allow_network_tts=False:
                        [Check("ffmpeg", True, "ok", None, True), Check("llm api key", False, "unset", "export it", False)])
    assert cli.main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "WARN" in out and "llm api key" in out
    monkeypatch.setattr(cli, "run_checks", lambda cfg, require_key, allow_network_tts=False:
                        [Check("ffmpeg", False, "missing", "brew install ffmpeg", True)])
    assert cli.main(["doctor"]) == 7


def test_run_returns_report_exit_code(monkeypatch, tmp_path):
    from codecast.report import ReportBuilder
    from codecast.config import load_config
    from codecast.errors import IngestError

    def fake_run(opts, cfg, **kw):
        rb = ReportBuilder("r", opts.repo, cfg)
        rb.fail(IngestError("nope"), stage="ingest")
        return rb.finish()
    monkeypatch.setattr(cli, "pipeline_run", fake_run)
    assert cli.main(["run", "--repo", "x", "--out", str(tmp_path)]) == 2


def test_bad_config_is_exit_7(tmp_path):
    assert cli.main(["run", "--repo", "x", "--out", str(tmp_path), "--config", str(tmp_path / "nope.yaml")]) == 7


def test_from_stage_requires_run_id(tmp_path):
    assert cli.main(["run", "--repo", "x", "--out", str(tmp_path), "--from-stage", "render",
                     "--workspace", str(tmp_path / "ws")]) == 7


def test_dotenv_in_cwd_is_loaded(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("CODECAST_DURATION_MAX_S", raising=False)
    (tmp_path / ".env").write_text("CODECAST_DURATION_MAX_S=ten\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert cli.main(["doctor"]) == 7
    assert "CODECAST_DURATION_MAX_S='ten'" in capsys.readouterr().err



def _capture_cfg(monkeypatch):
    from codecast.errors import IngestError
    from codecast.report import ReportBuilder
    seen = {}

    def fake_run(opts, cfg, **kw):
        seen["cfg"] = cfg
        rb = ReportBuilder("r", opts.repo, cfg)
        rb.fail(IngestError("stop here"), stage="ingest")
        return rb.finish()
    monkeypatch.setattr(cli, "pipeline_run", fake_run)
    return seen


def test_voice_model_flag_beats_env(monkeypatch, tmp_path):
    seen = _capture_cfg(monkeypatch)
    monkeypatch.setenv("CODECAST_VOICE_MODEL", "from-env.onnx")
    cli.main(["run", "--repo", "x", "--out", str(tmp_path), "--voice-model", "flag.onnx"])
    assert seen["cfg"].voice.model_path == "flag.onnx"


def test_env_voice_used_without_flag(monkeypatch, tmp_path):
    seen = _capture_cfg(monkeypatch)
    monkeypatch.setenv("CODECAST_VOICE_MODEL", "from-env.onnx")
    cli.main(["run", "--repo", "x", "--out", str(tmp_path)])
    assert seen["cfg"].voice.model_path == "from-env.onnx"



def test_failure_output_lists_paths(monkeypatch, tmp_path, capsys):
    from codecast.errors import CodecastError
    from codecast.report import ReportBuilder

    def fake_run(opts, cfg, **kw):
        rb = ReportBuilder("r1", opts.repo, cfg)
        rb.fail(CodecastError("agent step 20: OpenAI rejected the request (HTTP 400 x): y\n  request sent: z.json",
                              stage="agent"), stage="agent")
        return rb.finish()
    monkeypatch.setattr(cli, "pipeline_run", fake_run)
    ws = tmp_path / "ws"
    assert cli.main(["run", "--repo", "x", "--out", str(tmp_path), "--workspace", str(ws)]) == 1
    err = capsys.readouterr().err.splitlines()
    assert err[0] == "codecast FAILED (exit 1) at stage agent"
    assert err[1] == "  agent step 20: OpenAI rejected the request (HTTP 400 x): y"
    assert err[2] == "  request sent: z.json"
    assert err[3] == f"  full log: {ws / 'r1' / 'run.log'}"
    assert err[4] == f"  run report: {ws / 'r1' / 'run_report.json'}"


def test_compute_video_length_needs_no_repo(monkeypatch, tmp_path, capsys):
    from codecast import estimate
    from tests.fakes import SilenceEngine
    from tests.unit.test_estimate import write_storyboard
    write_storyboard(tmp_path / "run1")
    monkeypatch.setattr(estimate, "select_engine", lambda cfg, allow_network: SilenceEngine(0.4))
    monkeypatch.setattr(cli, "pipeline_run", lambda *a, **k: pytest.fail("pipeline must not run"))
    assert cli.main(["run", "--run", "run1", "--compute-video-length", "--workspace", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "total" in out and f"saved: {tmp_path / 'run1' / 'length_estimate.json'}" in out


def test_compute_video_length_requires_run(capsys):
    assert cli.main(["run", "--compute-video-length"]) == 7
    assert "--run" in capsys.readouterr().err


def test_compute_video_length_rejects_from_stage():
    assert cli.main(["run", "--run", "r", "--compute-video-length", "--from-stage", "voice"]) == 7


def test_run_without_repo_still_rejected(capsys):
    assert cli.main(["run"]) == 7
    assert "--repo" in capsys.readouterr().err
