import sys
import wave
from pathlib import Path

import pytest
import yaml

from codecast import preflight
from codecast.config import VoiceConfig, load_config
from codecast.errors import CodecastError, ConfigError
from codecast.voice.design import design_voice
from codecast.voice.voicefile import load_voice_file

FAKE = [sys.executable, str(Path(__file__).parents[1] / "fake_worker.py")]


def _cfg(tmp_path) -> VoiceConfig:
    return VoiceConfig(envs_dir=str(tmp_path / "envs"))


def _design(tmp_path, **kw):
    args = dict(command=FAKE, model="m", language="hi", instruct="calm", text="नमस्ते दुनिया।", options={},
                cfg=_cfg(tmp_path))
    args.update(kw)
    return design_voice(tmp_path / "voices" / "v.yaml", **args)


def test_design_writes_clip_and_voice_file(tmp_path):
    v = _design(tmp_path, options={"num_step": 16})
    clip = tmp_path / "voices" / "v.wav"
    with wave.open(str(clip)) as f:
        assert f.getnframes() > 0
    data = yaml.safe_load((tmp_path / "voices" / "v.yaml").read_text(encoding="utf-8"))
    assert data["reference"] == "v.wav" and data["reference_text"] == "नमस्ते दुनिया।"
    assert data["device"] == "auto" and data["options"] == {"num_step": 16} and data["language"] == "hi"
    again = load_voice_file(tmp_path / "voices" / "v.yaml")
    assert again.voice_id == v.voice_id and v.reference == clip.resolve()


def test_design_refuses_overwrite_without_force(tmp_path):
    _design(tmp_path)
    with pytest.raises(ConfigError, match="v.yaml"):
        _design(tmp_path)
    (tmp_path / "voices" / "v.yaml").unlink()
    with pytest.raises(ConfigError, match="v.wav"):
        _design(tmp_path)
    _design(tmp_path, force=True)


def test_design_failure_writes_nothing_and_closes(tmp_path):
    with pytest.raises(CodecastError):
        _design(tmp_path, options={"fail_design": True})
    assert not (tmp_path / "voices" / "v.yaml").exists()


def _voice_file(tmp_path, **extra):
    body = {"command": FAKE, "model": "m", "language": "hi", **extra}
    p = tmp_path / "fake.yaml"
    p.write_text(yaml.safe_dump(body), encoding="utf-8")
    return p


def _cfg_for(p):
    return load_config(env={"CODECAST_VOICE": f"model:{p}"})


def _quiet(monkeypatch):
    monkeypatch.setattr(preflight, "ffmpeg_problems", lambda: [])
    monkeypatch.setattr(preflight, "_chromium_problem", lambda: None)
    monkeypatch.setattr(preflight, "_developer_mode_on", lambda: True)


def _by_name(checks):
    return {c.name: c for c in checks}


def test_doctor_prints_command_of_voice(monkeypatch, tmp_path):
    _quiet(monkeypatch)
    checks = _by_name(preflight.run_checks(_cfg_for(_voice_file(tmp_path)), require_key=False))
    assert checks["voice command"].ok and "fake_worker.py" in checks["voice command"].message


def test_doctor_deep_reports_ready(monkeypatch, tmp_path):
    _quiet(monkeypatch)
    cfg = _cfg_for(_voice_file(tmp_path))
    cfg.voice.envs_dir = str(tmp_path / "envs")
    assert "voice worker" not in _by_name(preflight.run_checks(cfg, require_key=False))
    w = _by_name(preflight.run_checks(cfg, require_key=False, deep=True))["voice worker"]
    assert w.ok and "ready in" in w.message


def test_doctor_warns_developer_mode_off(monkeypatch, tmp_path):
    _quiet(monkeypatch)
    monkeypatch.setattr(preflight.sys, "platform", "win32")
    # a faked win32 breaks shutil.which on POSIX, so skip the say probe
    monkeypatch.setattr("codecast.voice.engines._say_available", lambda voice: False)
    monkeypatch.setattr(preflight, "_developer_mode_on", lambda: False)
    c = _by_name(preflight.run_checks(_cfg_for(_voice_file(tmp_path)), require_key=False))["developer mode"]
    assert not c.ok and not c.fatal and "copies files instead of linking" in c.message


def test_doctor_warns_voice_without_reference(monkeypatch, tmp_path):
    _quiet(monkeypatch)
    c = _by_name(preflight.run_checks(_cfg_for(_voice_file(tmp_path)), require_key=False))["voice reference"]
    assert not c.ok and not c.fatal


def test_failed_design_after_write_leaves_nothing(tmp_path):
    with pytest.raises(CodecastError):
        _design(tmp_path, options={"fail_design_after_write": True})
    assert list((tmp_path / "voices").iterdir()) == []


def test_failed_redesign_with_force_keeps_old_voice(tmp_path):
    _design(tmp_path)
    folder = tmp_path / "voices"
    before = {n: (folder / n).read_bytes() for n in ("v.yaml", "v.wav")}
    with pytest.raises(CodecastError):
        _design(tmp_path, force=True, options={"fail_design_after_write": True})
    assert {n: (folder / n).read_bytes() for n in ("v.yaml", "v.wav")} == before
    assert sorted(p.name for p in folder.iterdir()) == ["v.wav", "v.yaml"]


def _design_cli(tmp_path, worker, capsys):
    from codecast import cli
    argv = ["voice-design", str(tmp_path / "new" / "v.yaml"), "--worker", worker, "--model", "m", "--language", "hi",
            "--instruct", "x", "--text", "t"]
    code = cli.main(argv)
    return code, capsys.readouterr().err


def test_cli_unknown_worker_is_config_error(tmp_path, capsys):
    code, err = _design_cli(tmp_path, "foo", capsys)
    assert code == 7 and "unknown worker 'foo'" in err and "omnivoice" in err
    assert not (tmp_path / "new").exists()


def test_cli_missing_environment_is_config_error(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("CODECAST_VOICE_ENVS_DIR", str(tmp_path / "noenvs"))
    from codecast.voice import model_engine
    monkeypatch.setattr(model_engine, "interpreter_for", lambda worker, cfg: tmp_path / "nope" / "python.exe")
    code, err = _design_cli(tmp_path, "omnivoice", capsys)
    assert code == 7 and "codecast voice-env omnivoice" in err
    assert not (tmp_path / "new").exists()


def test_cli_design_prints_worker_progress(tmp_path, capsys, monkeypatch):
    from codecast import cli
    from codecast.voice import model_engine, voicefile
    monkeypatch.setattr(model_engine, "interpreter_for", lambda worker, cfg: Path(sys.executable))
    monkeypatch.setattr(voicefile, "WORKERS_DIR", Path(FAKE[1]).parent)
    monkeypatch.setitem(voicefile.SHIPPED_WORKERS, "omnivoice", Path(FAKE[1]).name)
    code = cli.main(["voice-design", str(tmp_path / "new" / "v.yaml"), "--worker", "omnivoice", "--model", "m",
                     "--language", "hi", "--instruct", "x", "--text", "t", "--option", "progress=loading the model"])
    err = capsys.readouterr().err
    assert code == 0, err
    assert "voice worker: loading the model" in err


def test_design_sends_an_absolute_out(tmp_path, monkeypatch):
    from codecast.voice.worker import Worker
    sent: list[dict] = []
    real = Worker.request

    def spy(self, op, **fields):
        sent.append({"op": op, **fields})
        return real(self, op, **fields)

    monkeypatch.setattr(Worker, "request", spy)
    monkeypatch.chdir(tmp_path)
    design_voice(Path("voices") / "rel.yaml", command=FAKE, model="m", language="hi", instruct="calm", text="t",
                 options={}, cfg=_cfg(tmp_path))
    out = next(s["out"] for s in sent if s["op"] == "design")
    assert Path(out).is_absolute() and Path(out).parent == (tmp_path / "voices").resolve()
