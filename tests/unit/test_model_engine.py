"""The `model` engine (spec 5.3, 5.4): lazy start, one worker for the engine's life, failures, the registry kind."""
from __future__ import annotations

import sys
import wave
from pathlib import Path

import pytest
import yaml

from codecast.config import VoiceConfig, load_config
from codecast.errors import CodecastError
from codecast.models import Storyboard, Submission
from codecast.voice import engines, registry
from codecast.voice.model_engine import ModelEngine, interpreter_for
from codecast.voice.timeline import synthesize_storyboard
from codecast.voice.voicefile import load_voice_file

from tests.unit.storyboards import valid_submission

FAKE = Path(__file__).resolve().parent.parent / "fake_worker.py"


def voice_path(folder: Path, name: str = "fake", device: str = "cpu", **options) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    p = folder / f"{name}.yaml"
    p.write_text(yaml.safe_dump({"command": [sys.executable, str(FAKE)], "device": device, "options": options}),
                 encoding="utf-8")
    return p


def vcfg(tmp_path, **kw) -> VoiceConfig:
    kw = {"worker_start_timeout_s": 30, "worker_sentence_timeout_s": 30, **kw}
    return VoiceConfig(envs_dir=str(tmp_path / "envs"), **kw)


def engine(tmp_path, cfg=None, **options) -> ModelEngine:
    return ModelEngine(load_voice_file(voice_path(tmp_path / "voices", **options)), cfg or vcfg(tmp_path))


def storyboard() -> Storyboard:
    sub = Submission.model_validate(valid_submission())
    return Storyboard(repo_url="u", commit_sha="c", title=sub.title, sources={s.id: s for s in sub.sources},
                      scenes=sub.scenes)


def rate(path: Path) -> int:
    with wave.open(str(path)) as w:
        return w.getframerate()


def test_lazy_start(tmp_path):
    e = engine(tmp_path)
    try:
        assert e._worker is None and e.ready is None
        e.synthesize("Hello there.", tmp_path / "a.wav")
        assert e._worker is not None and e._worker._proc.poll() is None
        assert e.ready["worker"] == "fake"
        assert rate(tmp_path / "a.wav") == 48000
    finally:
        e.close()


def test_one_process_across_calls(tmp_path):
    e = engine(tmp_path)
    try:
        e.synthesize("One.", tmp_path / "1.wav")
        pid = e._worker._proc.pid
        e.synthesize("Two.", tmp_path / "2.wav")
        synthesize_storyboard(storyboard(), e, tmp_path / "round1", gap_s=0.15, tail_s=0.5)
        assert e._worker._proc.pid == pid
    finally:
        e.close()


def test_all_cached_run_starts_no_worker(tmp_path):
    first = engine(tmp_path)
    try:
        synthesize_storyboard(storyboard(), first, tmp_path / "cache", gap_s=0.15, tail_s=0.5)
    finally:
        first.close()
    second = engine(tmp_path)
    try:
        tl = synthesize_storyboard(storyboard(), second, tmp_path / "cache", gap_s=0.15, tail_s=0.5)
        assert tl.total > 0
        assert second._worker is None
    finally:
        second.close()


def test_close_quits(tmp_path):
    e = engine(tmp_path)
    e.synthesize("Hi.", tmp_path / "a.wav")
    proc = e._worker._proc
    e.close()
    assert proc.poll() is not None
    e.close()


def test_close_never_raises(tmp_path, monkeypatch):
    e = engine(tmp_path)
    e.synthesize("Hi.", tmp_path / "a.wav")
    real = e._worker.close

    def boom():
        real()
        raise RuntimeError("close blew up")

    monkeypatch.setattr(e._worker, "close", boom)
    e.close()  # logged, not raised


def test_sentence_timeout_fails_voice_stage(tmp_path):
    e = engine(tmp_path, vcfg(tmp_path, worker_sentence_timeout_s=1.5), sleep_synth=60)
    try:
        with pytest.raises(CodecastError) as err:
            synthesize_storyboard(storyboard(), e, tmp_path / "c", gap_s=0.15, tail_s=0.5)
        assert err.value.stage == "voice"
        assert "worker_sentence_timeout_s" in err.value.message
        assert e._worker._proc.poll() is not None  # the worker was killed
    finally:
        e.close()


def test_crash_reports_stderr_tail(tmp_path):
    sentence = "This sentence makes the worker fall over."
    e = engine(tmp_path, crash_on=sentence, stdout_noise=True)
    try:
        e.synthesize("A fine one.", tmp_path / "ok.wav")
        with pytest.raises(CodecastError) as err:
            e.synthesize(sentence, tmp_path / "bad.wav")
        assert err.value.stage == "voice"
        assert "noise from a native write" in err.value.message or "noise during synth" in err.value.message
        assert sentence in err.value.message
        with pytest.raises(CodecastError):  # not restarted
            e.synthesize("Another.", tmp_path / "again.wav")
    finally:
        e.close()


def test_hindi_text_and_spaced_path_round_trip(tmp_path):
    cache = tmp_path / "a b" / "नमस्ते"
    cache.mkdir(parents=True)
    e = engine(tmp_path)
    try:
        out = cache / "वाक्य.wav"
        e.synthesize("नमस्ते, मैं इस श्रृंखला में आपका मार्गदर्शन करूँगी।", out)
        assert out.is_file() and rate(out) == 48000
        assert list(cache.iterdir()) == [out]  # the worker's raw file is cleaned up
    finally:
        e.close()


def test_cuda_oom_hint(tmp_path):
    workers = Path(__file__).resolve().parents[2] / "src" / "codecast" / "voice" / "workers"
    py = tmp_path / "oom.py"
    py.write_text(f"import sys\nsys.path.insert(0, r'{workers}')\nimport _protocol\n_protocol.setup_streams()\n"
                  "class H:\n"
                  "    def hello(self, voice):\n        return {'worker': 'oom'}\n"
                  "    def synthesize(self, text, seed, out):\n"
                  "        raise RuntimeError('CUDA out of memory. Tried to allocate 2.00 GiB')\n"
                  "_protocol.serve(H())\n", encoding="utf-8")
    vf = tmp_path / "oom.yaml"
    vf.write_text(yaml.safe_dump({"command": [sys.executable, str(py)], "device": "cuda"}), encoding="utf-8")
    e = ModelEngine(load_voice_file(vf), vcfg(tmp_path))
    try:
        with pytest.raises(CodecastError) as err:
            e.synthesize("Hi.", tmp_path / "a.wav")
    finally:
        e.close()
    assert "CUDA out of memory" in err.value.message and "another CodeCast run" in err.value.message
    from codecast.voice import worker as worker_mod
    assert not worker_mod._HELD  # released on close


def test_seed_is_sent(tmp_path, monkeypatch):
    from codecast.voice.voicefile import sentence_seed
    e = engine(tmp_path)
    sent: list[dict] = []
    try:
        e.synthesize("Warm up.", tmp_path / "w.wav")
        real = e._worker.request

        def spy(op, **fields):
            sent.append({"op": op, **fields})
            return real(op, **fields)

        monkeypatch.setattr(e._worker, "request", spy)
        e.synthesize("Seeded.", tmp_path / "s.wav")
    finally:
        e.close()
    assert sent[0]["op"] == "synthesize" and sent[0]["seed"] == sentence_seed("Seeded.")


def test_ready_recorded_in_report(tmp_repo_ws):
    from codecast.pipeline import RunOptions, run
    tmp_path, cfg = tmp_repo_ws
    e = ModelEngine(load_voice_file(voice_path(tmp_path / "voices")), cfg.voice)

    class StopRenderer:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def render_scene(self, *a, **k):
            raise RuntimeError("stop after voicing")

    try:
        rep = run(RunOptions(repo="https://github.com/o/tiny", out_dir=tmp_path / "o", workspace=tmp_path / "ws",
                             run_id="r1", from_stage="voice"), cfg, engine=e, renderer_factory=StopRenderer,
                  preflight=False)
    finally:
        e.close()
    assert rep.failure is not None and rep.failure.stage == "render", rep.failure
    assert rep.tts_engine == "model" and rep.tts_voice == e.voice_id
    assert rep.tts_worker == {"worker": "fake", "model": "fake", "device": "cpu", "versions": {}}


@pytest.fixture
def tmp_repo_ws(tmp_path, tiny_repo):
    ws = tmp_path / "ws" / "r1"
    ws.mkdir(parents=True)
    (ws / "repomap.json").write_text(tiny_repo.model_dump_json(), encoding="utf-8")
    (ws / "storyboard.json").write_text(storyboard().model_dump_json(), encoding="utf-8")
    cfg = load_config(env={"CODECAST_DURATION_MIN_S": "1"})
    cfg.voice.envs_dir = str(tmp_path / "envs")
    return tmp_path, cfg


# --- the registry kind ----------------------------------------------------------------------------------------------

def test_interpreter_for(tmp_path):
    c = vcfg(tmp_path)
    expected = ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    assert interpreter_for("omnivoice", c) == tmp_path / "envs" / "omnivoice" / expected
    c.worker_python = {"omnivoice": str(tmp_path / "py")}
    assert interpreter_for("omnivoice", c) == tmp_path / "py"


def test_model_kind_registered_and_status(tmp_path):
    kind = registry.kinds()["model"]
    c = vcfg(tmp_path)
    good = voice_path(tmp_path / "v")
    assert kind.status(str(good), c, allow_network=False) == "ok"
    bad = tmp_path / "v" / "bad.yaml"
    bad.write_text(yaml.safe_dump({"command": ["no-such-program-xyz"]}), encoding="utf-8")
    assert "no-such-program-xyz" in kind.status(str(bad), c, allow_network=False)
    assert "not" in kind.status(str(tmp_path / "missing.yaml"), c, allow_network=False)
    assert engines.engine_status(c, allow_network=False)["model"] == "ok"


def test_model_kind_status_needs_shipped_interpreter(tmp_path, monkeypatch):
    from codecast.voice import voicefile
    (tmp_path / "workers").mkdir()
    (tmp_path / "workers" / "omnivoice_worker.py").write_text("# stub\n")
    monkeypatch.setattr(voicefile, "WORKERS_DIR", tmp_path / "workers")
    vf = tmp_path / "omni.yaml"
    vf.write_text("worker: omnivoice\nmodel: k2-fsa/OmniVoice\n", encoding="utf-8")
    st = registry.kinds()["model"].status(str(vf), vcfg(tmp_path), allow_network=False)
    assert "interpreter" in st and "voice-env" in st


def test_select_engine_model(tmp_path):
    c = vcfg(tmp_path)
    c.voice = f"model:{voice_path(tmp_path / 'v')}"
    e = engines.select_engine(c, allow_network=False)
    try:
        assert isinstance(e, ModelEngine) and e.name == "model" and e._worker is None
    finally:
        e.close()
