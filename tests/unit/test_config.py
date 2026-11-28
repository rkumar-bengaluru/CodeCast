from pathlib import Path

import pytest

from codecast.config import load_config
from codecast.errors import ConfigError


def write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "c.yaml"
    p.write_text(text)
    return p


def test_defaults_load():
    cfg = load_config()
    assert cfg.llm.provider == "anthropic"
    assert cfg.llm.model == "claude-sonnet-5-5"
    assert cfg.llm.temperature == 0.3
    assert cfg.llm.max_tokens == 4096
    assert cfg.llm.timeout_s == 120
    assert cfg.voice.gap_s == 0.15
    assert cfg.voice.tail_s == 0.5
    assert "*.log" in cfg.render.terminal_output_allowlist


def test_user_file_deep_merges(tmp_path):
    cfg = load_config(write(tmp_path, "llm:\n  temperature: 0.1\n"))
    assert cfg.llm.temperature == 0.1
    assert cfg.llm.model == "claude-sonnet-5-5"


def test_openai_requires_explicit_model(tmp_path):
    with pytest.raises(ConfigError, match="model"):
        load_config(write(tmp_path, "llm:\n  provider: openai\n"))


def test_openai_defaults_key_env(tmp_path):
    cfg = load_config(write(tmp_path, "llm:\n  provider: openai\n  model: some-model\n"))
    assert cfg.llm.api_key_env == "OPENAI_API_KEY"


def test_unknown_provider_rejected(tmp_path):
    with pytest.raises(ConfigError, match="provider"):
        load_config(write(tmp_path, "llm:\n  provider: nope\n  model: x\n"))


def test_extended_thinking_rejected(tmp_path):
    with pytest.raises(ConfigError, match="tool_choice"):
        load_config(write(tmp_path, "llm:\n  extended_thinking: true\n"))


def test_missing_file_is_config_error(tmp_path):
    with pytest.raises(ConfigError):
        load_config(tmp_path / "absent.yaml")


def test_pricing_parsed(tmp_path):
    cfg = load_config(write(tmp_path, (
        "pricing:\n  m1:\n    input_per_mtok: 3\n    output_per_mtok: 15\n"
        "    cache_read_per_mtok: 0.3\n    cache_write_per_mtok: 3.75\n")))
    assert cfg.pricing["m1"].output_per_mtok == 15


def test_non_mapping_llm_section_is_config_error(tmp_path):
    with pytest.raises(ConfigError, match="llm"):
        load_config(write(tmp_path, "llm: foo\n"))


def test_null_llm_section_is_config_error(tmp_path):
    with pytest.raises(ConfigError, match="llm"):
        load_config(write(tmp_path, "llm: null\n"))


def test_list_voice_section_is_config_error(tmp_path):
    with pytest.raises(ConfigError, match="voice"):
        load_config(write(tmp_path, "voice: [1]\n"))


def write_yaml(tmp_path, data):
    import yaml
    p = tmp_path / "cfg.yaml"
    p.write_text(yaml.safe_dump(data), encoding="utf-8")
    return p


def test_voice_from_env_and_config(tmp_path):
    from codecast.config import resolved_voice
    cfg = load_config(write_yaml(tmp_path, {"voice": {"voice": "say:Samantha"}}), env={})
    assert resolved_voice(cfg.voice) == "say:Samantha"
    cfg = load_config(write_yaml(tmp_path, {"voice": {"voice": "say:Samantha"}}), env={"CODECAST_VOICE": "say:Alex"})
    assert resolved_voice(cfg.voice) == "say:Alex"            # env beats config file


def test_strongest_layer_wins_across_settings(tmp_path):
    from codecast.config import resolved_voice
    cfg = load_config(write_yaml(tmp_path, {"voice": {"voice": "say:Samantha"}}),
                      env={"CODECAST_VOICE_MODEL": str(tmp_path / "v.onnx")})
    assert resolved_voice(cfg.voice) == f"piper:{tmp_path / 'v.onnx'}"


def test_same_layer_conflict_is_error(tmp_path):
    from codecast.config import resolved_voice
    cfg = load_config(None, env={"CODECAST_VOICE": "say:Alex", "CODECAST_VOICE_MODEL": "x.onnx"})
    with pytest.raises(ConfigError, match="CODECAST_VOICE.*CODECAST_VOICE_MODEL"):
        resolved_voice(cfg.voice)


def test_model_voice_path_made_absolute(tmp_path, monkeypatch):
    from codecast.config import resolved_voice
    monkeypatch.chdir(tmp_path)
    cfg = load_config(None, env={"CODECAST_VOICE": "model:voices/hi.yaml"})
    assert resolved_voice(cfg.voice) == f"model:{Path.cwd() / 'voices' / 'hi.yaml'}"


def test_no_voice_means_probe():
    from codecast.config import resolved_voice
    assert resolved_voice(load_config(None, env={}).voice) is None


def test_source_fields_not_dumped_and_worker_defaults():
    cfg = load_config(None, env={"CODECAST_VOICE": "say:Alex"})
    dump = cfg.voice.model_dump()
    assert "voice_source" not in dump and "model_path_source" not in dump
    assert cfg.voice.worker_start_timeout_s == 1800 and cfg.voice.worker_python == {}


def test_paths_absolute_from_load_on(tmp_path, monkeypatch):
    from codecast.config import resolved_voice
    first = tmp_path / "a"
    first.mkdir()
    monkeypatch.chdir(first)
    cfg = load_config(None, env={"CODECAST_VOICE": "model:voices/hi.yaml"})
    monkeypatch.chdir(tmp_path)
    assert resolved_voice(cfg.voice) == f"model:{first.resolve() / 'voices' / 'hi.yaml'}" or \
        resolved_voice(cfg.voice) == f"model:{Path(first) / 'voices' / 'hi.yaml'}"


def test_voice_given_at_construction_is_the_config_layer():
    """VoiceConfig(...), model_validate and a dump read back keep the named voice (never silently the probe)."""
    from codecast.config import Config, VoiceConfig, resolved_voice
    assert resolved_voice(VoiceConfig(voice="model:/v/x.yaml")) == "model:/v/x.yaml"
    assert resolved_voice(VoiceConfig(model_path="/v/m.onnx")) == "piper:/v/m.onnx"
    data = load_config(None, env={}).model_dump()
    data["voice"]["voice"] = "say:Alex"
    cfg = Config.model_validate(data)
    assert resolved_voice(cfg.voice) == "say:Alex" and cfg.voice.voice_source == "config"
    loaded = load_config(None, env={"CODECAST_VOICE": "say:Bob"})
    assert loaded.voice.voice_source == "env"  # load_config records the real layer
    again = Config.model_validate(loaded.model_dump())
    assert resolved_voice(again.voice) == "say:Bob"
    assert VoiceConfig().voice_source is None and resolved_voice(VoiceConfig()) is None
