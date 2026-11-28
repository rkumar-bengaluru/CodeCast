from pathlib import Path

import pytest

from codecast.config import VoiceConfig
from codecast.errors import ConfigError
from codecast.voice import engines
from codecast.voice.engines import SayEngine, select_engine, wav_duration
from codecast.voice.fetch_voice import fetch_voice, voice_urls

from tests.fakes import SilenceEngine


def test_wav_duration(tmp_path):
    p = tmp_path / "a.wav"
    SilenceEngine(0.5).synthesize("two words", p)
    assert wav_duration(p) == pytest.approx(1.0)


def test_say_passes_text_via_file(monkeypatch, tmp_path):
    """Review Focus 3: text never becomes an argv flag."""
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"] = argv
        seen["text"] = Path(argv[argv.index("-f") + 1]).read_text()
        Path(argv[argv.index("-o") + 1]).write_bytes(b"")
    monkeypatch.setattr(engines.subprocess, "run", fake_run)
    monkeypatch.setattr(engines, "convert_to_wav48k", lambda src, dst: None)
    SayEngine("Samantha").synthesize('-v Evil "quoted" <tag> & 🎬', tmp_path / "o.wav")
    # `<`/`>` are spoken as words (see SayEngine); everything else reaches say verbatim.
    assert seen["text"] == '-v Evil "quoted"  less than tag greater than  & 🎬'
    assert "-v Evil" not in seen["argv"] and "<" not in seen["text"]
    assert all("quoted" not in a for a in seen["argv"])


def cfg(tmp_path):
    return VoiceConfig(voices_dir=str(tmp_path / "voices"))


def test_select_prefers_piper_when_model_present(monkeypatch, tmp_path):
    c = cfg(tmp_path)
    monkeypatch.setattr(engines, "_piper_installed", lambda: True)
    engines.piper_model_path(c).parent.mkdir(parents=True)
    engines.piper_model_path(c).write_bytes(b"x")
    monkeypatch.setattr(engines, "PiperEngine", lambda path: type("P", (), {"name": "piper"})())
    assert select_engine(c, allow_network=False).name == "piper"


def test_select_falls_back_to_say(monkeypatch, tmp_path):
    monkeypatch.setattr(engines, "_piper_installed", lambda: False)
    monkeypatch.setattr(engines, "_say_available", lambda voice: True)
    assert select_engine(cfg(tmp_path), allow_network=False).name == "say"


def test_edge_only_with_network_flag(monkeypatch, tmp_path):
    monkeypatch.setattr(engines, "_piper_installed", lambda: False)
    monkeypatch.setattr(engines, "_say_available", lambda voice: False)
    monkeypatch.setattr(engines, "_edge_installed", lambda: True)
    with pytest.raises(ConfigError, match="fetch-voice"):
        select_engine(cfg(tmp_path), allow_network=False)
    assert select_engine(cfg(tmp_path), allow_network=True).name == "edge-tts"


def test_engine_status(monkeypatch, tmp_path):
    monkeypatch.setattr(engines, "_piper_installed", lambda: True)
    monkeypatch.setattr(engines, "_say_available", lambda voice: True)
    monkeypatch.setattr(engines, "_edge_installed", lambda: False)
    st = engines.engine_status(cfg(tmp_path), allow_network=False)
    assert st["piper"].startswith("voice missing") and st["say"] == "ok" and st["edge-tts"] != "ok"


def test_voice_urls():
    urls = voice_urls("en_US-lessac-medium")
    assert urls[0].endswith("/en/en_US/lessac/medium/en_US-lessac-medium.onnx")
    assert urls[1].endswith(".onnx.json")


def test_fetch_voice_without_piper_is_a_notice(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(engines, "_piper_installed", lambda: False)
    assert fetch_voice(cfg(tmp_path)) is None
    assert "say" in capsys.readouterr().out


def test_fetch_voice_downloads_atomically(monkeypatch, tmp_path):
    monkeypatch.setattr(engines, "_piper_installed", lambda: True)

    class Resp:
        def __init__(self, url): self.url = url
        def read(self, n=-1):
            data, self.url = (self.url.encode(), "") if self.url else (b"", "")
            return data
        def __enter__(self): return self
        def __exit__(self, *a): return False
    path = fetch_voice(cfg(tmp_path), opener=lambda url, timeout=60: Resp(url))
    assert path.name == "en_US-lessac-medium.onnx" and path.read_bytes().endswith(b".onnx")
    assert not list(path.parent.glob("*.part"))


def custom(tmp_path, json=True):
    m = tmp_path / "me.onnx"
    m.write_bytes(b"x")
    if json:
        (tmp_path / "me.onnx.json").write_text("{}")
    return m


def test_custom_model_path_wins(tmp_path):
    m = custom(tmp_path)
    assert engines.piper_model_path(VoiceConfig(model_path=str(m), voices_dir=str(tmp_path / "v"))) == m


def test_custom_voice_selected_with_its_id(monkeypatch, tmp_path):
    import piper
    m = custom(tmp_path)
    monkeypatch.setattr(engines, "_piper_installed", lambda: True)
    monkeypatch.setattr(piper.PiperVoice, "load", staticmethod(lambda path: object()))
    eng = select_engine(VoiceConfig(model_path=str(m)), allow_network=False)
    assert (eng.name, eng.voice_id) == ("piper", "me")


def test_missing_custom_voice_is_an_error_not_a_fallback(monkeypatch, tmp_path):
    monkeypatch.setattr(engines, "_piper_installed", lambda: True)
    monkeypatch.setattr(engines, "_say_available", lambda v: True)
    with pytest.raises(ConfigError, match="custom voice not found"):
        select_engine(VoiceConfig(model_path=str(tmp_path / "nope.onnx")), allow_network=False)


def test_custom_voice_without_json_says_so(monkeypatch, tmp_path):
    monkeypatch.setattr(engines, "_piper_installed", lambda: True)
    m = custom(tmp_path, json=False)
    with pytest.raises(ConfigError, match=r"me\.onnx\.json"):
        select_engine(VoiceConfig(model_path=str(m)), allow_network=False)


def test_say_and_edge_voice_ids():
    assert SayEngine("Samantha").voice_id == "Samantha"
    assert engines.EdgeEngine("en-US-AriaNeural").voice_id == "en-US-AriaNeural"


# --- named voices and the registry (spec 5.2) ---------------------------------------------------------------------

def named(tmp_path, voice):
    c = cfg(tmp_path)
    c.voice = voice  # assignment is the flag layer
    return c


def test_named_piper_by_name_and_path(monkeypatch, tmp_path):
    import piper
    monkeypatch.setattr(engines, "_piper_installed", lambda: True)
    monkeypatch.setattr(piper.PiperVoice, "load", staticmethod(lambda path: object()))
    c = cfg(tmp_path)
    (tmp_path / "voices").mkdir()
    (tmp_path / "voices" / "amy.onnx").write_bytes(b"x")
    c.voice, c.model_path = "piper:amy", None
    assert select_engine(c, allow_network=False).voice_id == "amy"
    m = custom(tmp_path)
    c2 = cfg(tmp_path)
    c2.voice = f"piper:{m}"
    assert select_engine(c2, allow_network=False).voice_id == "me"
    c3 = cfg(tmp_path)
    c3.voice = "piper:ghost"
    with pytest.raises(ConfigError, match="ghost"):
        select_engine(c3, allow_network=False)


def test_named_say_voice(monkeypatch, tmp_path):
    monkeypatch.setattr(engines, "_say_available", lambda voice: voice == "Alex")
    c = cfg(tmp_path)
    c.voice = "say:Alex"
    eng = select_engine(c, allow_network=False)
    assert (eng.name, eng.voice_id) == ("say", "Alex")


def test_named_edge_needs_network(monkeypatch, tmp_path):
    monkeypatch.setattr(engines, "_edge_installed", lambda: True)
    c = cfg(tmp_path)
    c.voice = "edge-tts:en-GB-SoniaNeural"
    with pytest.raises(ConfigError, match="--allow-network-tts"):
        select_engine(c, allow_network=False)
    assert select_engine(c, allow_network=True).voice_id == "en-GB-SoniaNeural"


def test_unknown_engine(tmp_path):
    c = cfg(tmp_path)
    c.voice = "bogus:x"
    with pytest.raises(ConfigError, match=r"piper, say, edge-tts, model"):
        select_engine(c, allow_network=False)


def test_named_voice_never_falls_back(monkeypatch, tmp_path):
    monkeypatch.setattr(engines, "_piper_installed", lambda: True)
    monkeypatch.setattr(engines, "_say_available", lambda v: True)
    c = cfg(tmp_path)
    c.voice = "piper:missing"
    with pytest.raises(ConfigError):
        select_engine(c, allow_network=False)


def test_builtin_engines_close_is_a_noop_and_status_has_no_model_unnamed(tmp_path):
    assert SayEngine("x").close() is None
    assert "model" not in engines.engine_status(cfg(tmp_path), allow_network=False)


def test_probe_failure_message_word_for_word(monkeypatch, tmp_path):
    """R3: with no model voice named, the probe's message is today's, with the three built-in engines only."""
    monkeypatch.setattr(engines, "_piper_installed", lambda: False)
    monkeypatch.setattr(engines, "_say_available", lambda voice: False)
    monkeypatch.setattr(engines, "_edge_installed", lambda: False)
    with pytest.raises(ConfigError) as e:
        select_engine(cfg(tmp_path), allow_network=False)
    st = {"piper": "piper-tts not installed", "say": "macOS say or voice 'Samantha' unavailable",
          "edge-tts": "edge-tts not installed"}
    assert e.value.message == ("no TTS engine available: run `codecast fetch-voice` (piper), use macOS `say`, "
                               f"or pass --allow-network-tts with edge-tts installed. Status: {st}")


def test_engine_status_reports_named_model_voice_and_never_raises(tmp_path):
    c = cfg(tmp_path)
    c.voice = f"model:{tmp_path / 'missing.yaml'}"
    assert "missing.yaml" in engines.engine_status(c, allow_network=False)["model"]
    c.voice = "say:Alex"
    assert "model" not in engines.engine_status(c, allow_network=False)
    clash = VoiceConfig(voices_dir=str(tmp_path), voice=f"model:{tmp_path / 'v.yaml'}", model_path="x.onnx")
    st = engines.engine_status(clash, allow_network=False)  # a same-layer conflict: a status, not a raise
    assert "both name a voice" in st["model"]


# --- model_path keeps today's messages through the real config path (R3) --------------------------------------------

def _model_cfg(path, **env):
    from codecast.config import load_config
    return load_config(env={"CODECAST_VOICE_MODEL": str(path), **env})


def test_env_model_path_messages_word_for_word(monkeypatch, tmp_path):
    monkeypatch.setattr(engines, "_piper_installed", lambda: True)
    monkeypatch.setattr(engines, "_say_available", lambda v: True)
    c = _model_cfg(tmp_path / "nope.onnx")
    with pytest.raises(ConfigError) as e:
        select_engine(c.voice, allow_network=False)
    assert str(e.value) == f"custom voice not found: {tmp_path / 'nope.onnx'} (from --voice-model or CODECAST_VOICE_MODEL)"
    m = custom(tmp_path, json=False)
    with pytest.raises(ConfigError) as e:
        select_engine(_model_cfg(m).voice, allow_network=False)
    assert str(e.value) == (f"custom voice config missing: {m}.json (piper needs it next to the .onnx)"
                            " (from --voice-model or CODECAST_VOICE_MODEL)")
    monkeypatch.setattr(engines, "_piper_installed", lambda: False)
    with pytest.raises(ConfigError) as e:
        select_engine(_model_cfg(m).voice, allow_network=False)
    assert str(e.value) == "piper-tts not installed (from --voice-model or CODECAST_VOICE_MODEL)"


def test_flag_model_path_messages_word_for_word(monkeypatch, tmp_path):
    from codecast.config import apply_voice_flags, load_config
    monkeypatch.setattr(engines, "_piper_installed", lambda: True)
    c = load_config(env={})
    apply_voice_flags(c, voice=None, voice_model=str(tmp_path / "nope.onnx"))
    with pytest.raises(ConfigError) as e:
        select_engine(c.voice, allow_network=False)
    assert str(e.value) == f"custom voice not found: {tmp_path / 'nope.onnx'} (from --voice-model or CODECAST_VOICE_MODEL)"


def test_weaker_model_path_equal_to_flag_voice_is_not_the_model_message(monkeypatch, tmp_path):
    from codecast.config import apply_voice_flags
    monkeypatch.setattr(engines, "_piper_installed", lambda: True)
    p = tmp_path / "nope.onnx"
    c = _model_cfg(p)
    apply_voice_flags(c, voice=f"piper:{p}", voice_model=None)  # the flag layer beats the env layer
    with pytest.raises(ConfigError) as e:
        select_engine(c.voice, allow_network=False)
    assert "from --voice-model" not in str(e.value) and str(e.value).startswith("piper:")
