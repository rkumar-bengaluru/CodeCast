"""Clip scenes: storyboard shape, validation (ffprobe stubbed) and the compose filter."""

import logging
from pathlib import Path

import pytest

from codecast.compose.segments import CLIP_PAD_S, clip_filter, clip_speed, segment_command
from codecast.errors import RenderError
from codecast.grounding import spans
from codecast.grounding.validator import ValidationContext, validate_submission
from codecast.models import ClipScene, ClipSource, SceneFrames, Submission, VideoRef

from tests.unit.storyboards import CLIP_FILE, clip_submission


@pytest.fixture
def video(tiny_repo, monkeypatch):
    """A placeholder recording in the checkout; ffprobe is stubbed to report 10 s."""
    path = Path(tiny_repo.root) / CLIP_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"not really a video")
    monkeypatch.setattr(spans, "video_duration", lambda p: 10.0)
    return path


def run(repo, raw):
    return validate_submission(raw, ValidationContext(repomap=repo, llm={}, output_allowlist=[]))


def codes(res):
    return {i.code for i in res.issues}


def test_submission_parses_clip_and_video_source():
    sub = Submission.model_validate(clip_submission(1.0, 4.5))
    assert isinstance(sub.sources[-1], VideoRef) and sub.sources[-1].file == CLIP_FILE
    clip = sub.scenes[4]
    assert isinstance(clip, ClipScene) and (clip.video_id, clip.start_s, clip.end_s) == ("demo", 1.0, 4.5)


def test_valid_clip_storyboard(tiny_repo, video):
    res = run(tiny_repo, clip_submission(2.0, 9.5))
    assert res.ok, res.issues
    assert res.storyboard.scenes[4].type == "clip"


@pytest.mark.parametrize("start,end", [(-1, 2), (3, 3), (4, 2), (2, 10.5)])
def test_clip_range_must_fit_the_video(tiny_repo, video, start, end):
    res = run(tiny_repo, clip_submission(start, end))
    assert codes(res) == {"CLIP_RANGE_INVALID"}
    assert "10.000s" in res.issues[0].message


def test_clip_to_the_last_frame_is_allowed(tiny_repo, video):
    assert run(tiny_repo, clip_submission(0, 10.0)).ok


@pytest.mark.parametrize("file,code", [
    ("rec/missing.mp4", "VIDEO_NOT_FOUND"),
    ("../outside.mp4", "VIDEO_NOT_FOUND"),
    ("rec", "VIDEO_NOT_FOUND"),            # a folder
    ("README.md", "VIDEO_BAD_EXTENSION"),
])
def test_video_source_file_checks(tiny_repo, video, file, code):
    (Path(tiny_repo.root).parent / "outside.mp4").write_bytes(b"x")
    raw = clip_submission()
    raw["sources"][-1]["file"] = file
    res = run(tiny_repo, raw)
    assert code in codes(res) and "CLIP_RANGE_INVALID" not in codes(res)


def test_webm_is_accepted(tiny_repo, video):
    webm = video.with_suffix(".webm")
    webm.write_bytes(b"x")
    raw = clip_submission()
    raw["sources"][-1]["file"] = "rec/demo.webm"
    assert run(tiny_repo, raw).ok


def test_unreadable_video(tiny_repo, video, monkeypatch):
    def broken(path):
        raise ValueError("moov atom not found")
    monkeypatch.setattr(spans, "video_duration", broken)
    res = run(tiny_repo, clip_submission())
    assert codes(res) == {"VIDEO_UNREADABLE"} and "moov atom" in res.issues[0].message


def test_video_id_must_name_a_video_source(tiny_repo, video):
    raw = clip_submission()
    raw["scenes"][4]["video_id"] = "plot"
    assert "WRONG_SOURCE_KIND" in codes(run(tiny_repo, raw))
    raw["scenes"][4]["video_id"] = "nope"
    assert "UNKNOWN_SOURCE_ID" in codes(run(tiny_repo, raw))
    raw = clip_submission()
    raw["scenes"][3] = {"type": "image", "image_id": "demo", "narration": raw["scenes"][3]["narration"]}
    assert "WRONG_SOURCE_KIND" in codes(run(tiny_repo, raw))


def test_video_source_grounds_nothing(tiny_repo, video):
    raw = clip_submission()
    raw["scenes"][4]["narration"][0]["text"] = "The recording sorts 90 clips in the browser."
    res = run(tiny_repo, raw)
    assert codes(res) == {"UNGROUNDED_NUMBER"}
    raw["sources"].append({"kind": "json", "id": "n", "file": "metrics.json", "pointer": "/n_videos"})
    raw["scenes"][4]["narration"][0]["source_ids"] = ["demo", "n"]   # the number needs a citable source
    assert run(tiny_repo, raw).ok


def clip_frames(start: float, end: float, duration: float, **kw) -> SceneFrames:
    return SceneFrames(scene_id="scene05", frames=["/w/scene05.png"], motion="clip", duration=duration,
                       clip=ClipSource(path="/rec/demo.mp4", start=start, end=end), **kw)


def test_clip_speed():
    assert clip_speed(8.0, 4.0) == 2.0
    assert clip_speed(3.0, 4.0) == clip_speed(4.0, 4.0) == 1.0


def test_long_clip_is_sped_up():
    f = clip_filter(clip_frames(2.0, 10.0, 4.0))
    assert f == f"setpts=(PTS-STARTPTS)/2.000000,tpad=stop_mode=clone:stop_duration={CLIP_PAD_S:.3f}"


def test_short_clip_holds_its_last_frame():
    f = clip_filter(clip_frames(1.0, 2.5, 4.0))
    assert f == f"setpts=PTS-STARTPTS,tpad=stop_mode=clone:stop_duration={2.5 + CLIP_PAD_S:.3f}"


def test_speedup_over_4x_warns(caplog):
    with caplog.at_level(logging.WARNING, logger="codecast.compose"):
        clip_filter(clip_frames(0, 8.0, 2.0))
    assert not caplog.records
    with caplog.at_level(logging.WARNING, logger="codecast.compose"):
        clip_filter(clip_frames(0, 9.0, 2.0))
    assert "4.5x" in caplog.text and "scene05" in caplog.text


def test_clip_segment_command(tmp_path):
    from codecast.models import CaptionImage
    sf = clip_frames(1.25, 3.0, 4.0, captions=[CaptionImage(path="/w/c0.png", start=0.0, end=2.0)])
    argv, ftext, extra = segment_command(sf, tmp_path / "o.mp4", tmp_path / "f.txt", tmp_path)
    i = argv.index("-i")
    assert argv[i - 5:i + 2] == ["-ss", "1.250", "-t", "1.750", "-an", "-i", "/rec/demo.mp4"]
    assert ftext.startswith("[0:v]setpts=PTS-STARTPTS,tpad=stop_mode=clone:stop_duration=2.750,"
                            "scale=1920:1080:flags=lanczos,setsar=1,fps=30,format=rgba[v0];\n")
    assert "[v0][1:v]overlay=0:920" in ftext and ftext.endswith("[vout]")
    out_t = len(argv) - 1 - argv[::-1].index("-t")
    assert argv[out_t + 1] == "4.000" and extra == {}
    for flag, val in (("-c:v", "libx264"), ("-pix_fmt", "yuv420p"), ("-r", "30")):
        assert argv[argv.index(flag) + 1] == val
    assert argv[-2] == "-an"


def test_clip_motion_needs_a_clip(tmp_path):
    sf = SceneFrames(scene_id="s", frames=["/w/s.png"], motion="clip", duration=2.0)
    with pytest.raises(RenderError, match="needs a clip"):
        segment_command(sf, tmp_path / "o.mp4", tmp_path / "f.txt", tmp_path)
