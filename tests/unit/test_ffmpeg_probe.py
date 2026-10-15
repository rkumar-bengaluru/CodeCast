import pytest

from codecast.compose import ffmpeg as ff
from codecast.errors import RenderError


def info(fmt, v, a):
    return {"format": {"duration": str(fmt), "size": "1000"},
            "streams": [{"codec_type": "video", "duration": str(v), "width": 1920, "height": 1080},
                        {"codec_type": "audio", "duration": str(a)}]}


def test_media_durations():
    assert ff.media_durations(info(55.0, 55.0, 55.02)) == {"format": 55.0, "video": 55.0, "audio": 55.02}


@pytest.mark.parametrize("fmt,v,a,ok", [
    (55.0, 55.0, 55.02, True),
    (49.9, 49.9, 49.9, False),
    (60.2, 60.2, 60.2, False),
    (55.0, 55.0, 55.2, False),
])
def test_post_check(monkeypatch, tmp_path, fmt, v, a, ok):
    mp4 = tmp_path / "x.mp4"
    mp4.write_bytes(b"x")
    monkeypatch.setattr(ff, "probe", lambda p: info(fmt, v, a))
    if ok:
        assert ff.post_check(mp4)["duration_s"] == fmt
    else:
        with pytest.raises(RenderError) as ei:
            ff.post_check(mp4)
        assert ei.value.exit_code == 6


def test_run_ffmpeg_failure_carries_stderr(tmp_path):
    with pytest.raises(RenderError, match="No such file"):
        ff.run_ffmpeg(["ffmpeg", "-v", "error", "-i", str(tmp_path / "missing.png"), str(tmp_path / "o.mp4")])
