from __future__ import annotations

import hashlib
import re
import shutil
import unicodedata
from pathlib import Path

import pytest

from codecast.errors import ConfigError
from codecast.voice import voicefile
from codecast.voice.voicefile import load_voice_file, sentence_seed


@pytest.fixture(autouse=True)
def workers(tmp_path, monkeypatch):
    d = tmp_path / "_workers"
    d.mkdir()
    (d / "omnivoice_worker.py").write_bytes(b"# worker v1\n")
    (d / "omnivoice.requirements.txt").write_bytes(b"torch==1\n")
    monkeypatch.setattr(voicefile, "WORKERS_DIR", d)
    return d


def _write(folder: Path, body: str | None = None, clip: bytes = b"RIFFclip", name="hi-narrator.yaml") -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "ref.wav").write_bytes(clip)
    body = body or (
        "worker: omnivoice\nmodel: m/one\nlanguage: hi\ndevice: auto\nreference: ref.wav\n"
        "reference_text: hello\noptions: {num_step: 16, a: 1}\n"
    )
    p = folder / name
    p.write_text(body, encoding="utf-8")
    return p


def _id(folder, body=None, clip=b"RIFFclip", **kw):
    return load_voice_file(_write(folder, body, clip), **kw).voice_id


def test_unknown_key_rejected(tmp_path):
    p = _write(tmp_path, "worker: omnivoice\nbogus: 1\n")
    with pytest.raises(ConfigError, match="hi-narrator.yaml"):
        load_voice_file(p)


@pytest.mark.parametrize("body", ["model: x\n", "worker: omnivoice\ncommand: [a]\n"])
def test_worker_xor_command(tmp_path, body):
    with pytest.raises(ConfigError):
        load_voice_file(_write(tmp_path, body))


def test_platform_override_applies(tmp_path):
    body = "worker: omnivoice\nmodel: a\nplatforms:\n  darwin: {model: b}\n"
    p = _write(tmp_path, body)
    assert load_voice_file(p, platform="linux").model == "a"
    assert load_voice_file(p, platform="darwin").model == "b"


def test_platform_override_bad_key(tmp_path):
    p = _write(tmp_path, "worker: omnivoice\nplatforms:\n  darwin: {language: b}\n")
    with pytest.raises(ConfigError):
        load_voice_file(p, platform="darwin")


def test_voice_id_stable_across_move(tmp_path):
    a = _id(tmp_path / "a")
    b = load_voice_file(shutil.copytree(tmp_path / "a", tmp_path / "b") / "hi-narrator.yaml").voice_id
    assert a == b


BASE = "worker: omnivoice\nmodel: m/one\nlanguage: hi\ndevice: auto\nreference: ref.wav\nreference_text: hello\n"


@pytest.mark.parametrize(
    "what",
    ["clip", "model", "language", "device", "option", "reference_text", "platform", "worker_py", "worker_req"],
)
def test_voice_id_changes_with(tmp_path, workers, what):
    def body(**over):
        d = {"model": "m/one", "language": "hi", "device": "auto", "reference_text": "hello", "num_step": 16}
        d.update(over)
        return (
            f"worker: omnivoice\nmodel: {d['model']}\nlanguage: {d['language']}\ndevice: {d['device']}\n"
            f"reference: ref.wav\nreference_text: {d['reference_text']}\noptions: {{num_step: {d['num_step']}}}\n"
            + (d.get("extra") or "")
        )

    base = _id(tmp_path / "a", body())
    if what == "clip":
        other = _id(tmp_path / "b", body(), clip=b"other")
    elif what == "model":
        other = _id(tmp_path / "b", body(model="m/two"))
    elif what == "language":
        other = _id(tmp_path / "b", body(language="en"))
    elif what == "device":
        other = _id(tmp_path / "b", body(device="cpu"))
    elif what == "option":
        other = _id(tmp_path / "b", body(num_step=32))
    elif what == "reference_text":
        other = _id(tmp_path / "b", body(reference_text="bye"))
    elif what == "platform":
        other = _id(tmp_path / "b", body(extra="platforms:\n  linux: {device: cpu}\n"), platform="linux")
        base = _id(tmp_path / "a", body(), platform="linux")
    elif what == "worker_py":
        (workers / "omnivoice_worker.py").write_bytes(b"# worker v2\n")
        other = _id(tmp_path / "b", body())
    else:
        (workers / "omnivoice.requirements.txt").write_bytes(b"torch==2\n")
        other = _id(tmp_path / "b", body())
    assert base != other


def test_voice_id_ignores_worker_line_endings(tmp_path, workers):
    a = _id(tmp_path / "a")
    (workers / "omnivoice_worker.py").write_bytes(b"# worker v1\r\n")
    (workers / "omnivoice.requirements.txt").write_bytes(b"torch==1\r\n")
    assert _id(tmp_path / "b") == a


def test_voice_id_ignores_options_order(tmp_path):
    a = _id(tmp_path / "a", BASE + "options: {x: 1, y: 2}\n")
    b = _id(tmp_path / "b", BASE + "options: {y: 2, x: 1}\n")
    assert a == b


def test_voice_id_nfc(tmp_path):
    nfc = unicodedata.normalize("NFC", "é")
    nfd = unicodedata.normalize("NFD", "é")
    body = BASE + "options: {k: %s}\n"
    a = load_voice_file(_write(tmp_path / "a", body.replace("hello", nfc) % nfc))
    b = load_voice_file(_write(tmp_path / "b", body.replace("hello", nfd) % nfd))
    assert a.voice_id == b.voice_id
    assert b.reference_text == nfc and b.options == {"k": nfc}
    assert b.to_hello()["reference_text"] == nfc


def test_voice_id_format(tmp_path):
    assert re.fullmatch(r"hi-narrator-[0-9a-f]{12}", _id(tmp_path))


def test_command_keyed_on_argv(tmp_path):
    a = _id(tmp_path / "a", "command: [prog, '{voice_dir}/x']\n")
    b = _id(tmp_path / "b", "command: [prog, '{voice_dir}/x']\n")
    c = _id(tmp_path / "c", "command: [prog2, '{voice_dir}/x']\n")
    assert a == b != c
    r = load_voice_file(tmp_path / "a" / "hi-narrator.yaml")
    assert r.command == ["prog", f"{(tmp_path / 'a').resolve()}/x"]
    assert r.worker is None


def test_missing_reference_is_config_error(tmp_path):
    p = _write(tmp_path)
    (tmp_path / "ref.wav").unlink()
    with pytest.raises(ConfigError, match="ref.wav"):
        load_voice_file(p)


def test_unknown_worker_and_missing_py(tmp_path, workers):
    with pytest.raises(ConfigError):
        load_voice_file(_write(tmp_path, "worker: nope\n"))
    (workers / "omnivoice_worker.py").unlink()
    with pytest.raises(ConfigError, match="omnivoice_worker.py"):
        load_voice_file(_write(tmp_path, "worker: omnivoice\n"))


def test_to_hello(tmp_path):
    r = load_voice_file(_write(tmp_path))
    h = r.to_hello()
    assert h["worker"] == "omnivoice" and h["reference"] == str((tmp_path / "ref.wav").resolve())
    assert set(h) == {"worker", "model", "language", "device", "reference", "reference_text", "options"}


def test_sentence_seed_deterministic_and_nfc():
    assert sentence_seed("é") == sentence_seed(unicodedata.normalize("NFD", "é"))
    assert sentence_seed("a") == int.from_bytes(hashlib.sha256(b"a").digest()[:8], "big")
    assert sentence_seed("a") != sentence_seed("b")


def test_unknown_platform_key_rejected(tmp_path):
    with pytest.raises(ConfigError, match="windows"):
        load_voice_file(_write(tmp_path, "worker: omnivoice\nplatforms:\n  windows: {model: x}\n"))


@pytest.mark.parametrize("opts", ["{when: 2026-01-01}", "{1: a, b: c}"])
def test_options_must_be_json(tmp_path, opts):
    with pytest.raises(ConfigError, match="JSON"):
        load_voice_file(_write(tmp_path, f"worker: omnivoice\noptions: {opts}\n"))


def test_shipped_worker_files_do_not_shadow_their_model_package():
    from codecast.voice.voicefile import SHIPPED_WORKERS

    for name, filename in SHIPPED_WORKERS.items():
        stem = filename.removesuffix(".py")
        assert stem != name.replace("-", "_"), filename
        assert stem not in {"omnivoice", "mlx_audio"}, filename
