import os

import pytest

from codecast.config import load_config
from codecast.errors import ConfigError
from codecast.settings import load_env_file


def test_no_env_keeps_default_limits():
    cfg = load_config(env={})
    assert cfg.limits.duration_range == (50.0, 300.0)
    assert (cfg.limits.min_words, cfg.limits.max_words, cfg.limits.target_words) == (110, 200, 155)
    assert (cfg.limits.min_scenes, cfg.limits.max_scenes) == (4, 10)
    assert cfg.voice.model_path is None


def test_env_overrides_scene_count():
    cfg = load_config(env={"CODECAST_MIN_SCENES": "40", "CODECAST_MAX_SCENES": "60"})
    assert (cfg.limits.min_scenes, cfg.limits.max_scenes) == (40, 60)
    assert load_config(env={"CODECAST_MAX_SCENES": "60"}).limits.min_scenes == 4


def test_env_overrides_duration_only():
    cfg = load_config(env={"CODECAST_DURATION_MIN_S": "0", "CODECAST_DURATION_MAX_S": "600"})
    assert cfg.limits.duration_range == (0.0, 600.0)
    assert cfg.limits.max_words == 200


def test_env_overrides_words_and_voice():
    cfg = load_config(env={"CODECAST_MAX_WORDS": "800", "CODECAST_TARGET_WORDS": "600",
                           "CODECAST_VOICE_MODEL": "voices/me.onnx"})
    assert (cfg.limits.max_words, cfg.limits.target_words) == (800, 600)
    assert cfg.voice.model_path == "voices/me.onnx"


@pytest.mark.parametrize("env, needle", [
    ({"CODECAST_DURATION_MAX_S": "ten"}, "CODECAST_DURATION_MAX_S"),
    ({"CODECAST_MIN_WORDS": "1.5"}, "CODECAST_MIN_WORDS"),
    ({"CODECAST_DURATION_MIN_S": "400"}, "duration"),          # 400 > default max 300
    ({"CODECAST_DURATION_MIN_S": "-1"}, "duration"),
    ({"CODECAST_TARGET_WORDS": "500"}, "target_words"),
    ({"CODECAST_MAX_SCENES": "6.5"}, "CODECAST_MAX_SCENES"),
    ({"CODECAST_MIN_SCENES": "12"}, "min_scenes"),             # 12 > default max 10
    ({"CODECAST_MIN_SCENES": "1"}, "min_scenes"),
])
def test_bad_limits_fail_with_the_name(env, needle):
    with pytest.raises(ConfigError, match=needle):
        load_config(env=env)


def test_missing_env_file_is_fine(tmp_path):
    assert load_env_file(tmp_path / ".env") is False


def test_env_file_does_not_override_shell(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("CODECAST_MAX_WORDS=300\n", encoding="utf-8")
    monkeypatch.setenv("CODECAST_MAX_WORDS", "250")
    assert load_env_file(tmp_path / ".env") is True
    assert os.environ["CODECAST_MAX_WORDS"] == "250"


def test_env_file_windows_path_with_equals(tmp_path, monkeypatch):
    monkeypatch.delenv("CODECAST_VOICE_MODEL", raising=False)
    p = r"C:\2026\rkumar\CodeCast\voices\rupak-best-mos-epoch=44.onnx"
    (tmp_path / ".env").write_text(f'CODECAST_VOICE_MODEL="{p}"\n', encoding="utf-8")
    load_env_file(tmp_path / ".env")
    assert os.environ["CODECAST_VOICE_MODEL"] == p


def test_env_file_comments_quotes_and_export(tmp_path, monkeypatch):
    for k in ("CODECAST_A", "CODECAST_B", "CODECAST_C", "CODECAST_D"):
        monkeypatch.delenv(k, raising=False)
    (tmp_path / ".env").write_text(
        "# a comment\n\nexport CODECAST_A=1\nCODECAST_B = 'two words'\nCODECAST_C=3  # trailing\nnot a line\nCODECAST_D=\n",
        encoding="utf-8")
    load_env_file(tmp_path / ".env")
    assert (os.environ["CODECAST_A"], os.environ["CODECAST_B"], os.environ["CODECAST_C"]) == ("1", "two words", "3")
    assert os.environ["CODECAST_D"] == ""


def test_quoted_value_with_trailing_comment(tmp_path, monkeypatch):
    monkeypatch.delenv("CODECAST_VOICE_MODEL", raising=False)
    p = r"C:\2026\rkumar\voices\x=44.onnx"
    (tmp_path / ".env").write_text(f'CODECAST_VOICE_MODEL="{p}"   # my voice\n', encoding="utf-8")
    load_env_file(tmp_path / ".env")
    assert os.environ["CODECAST_VOICE_MODEL"] == p


def test_unreadable_env_file_is_a_config_error(tmp_path):
    (tmp_path / ".env").write_text("CODECAST_MAX_WORDS=300\n", encoding="utf-16")
    with pytest.raises(ConfigError, match=r"\.env.*UTF-8"):
        load_env_file(tmp_path / ".env")
