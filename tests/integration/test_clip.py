"""Clip scenes against a real (tiny) recording: ffprobe validation, the preview still and compose timing."""

import subprocess
from pathlib import Path

import pytest
from PIL import Image

from codecast.compose.ffmpeg import compose_video, probe
from codecast.grounding.spans import video_duration
from codecast.grounding.validator import ValidationContext, validate_submission
from codecast.models import ClipSource, SceneFrames, Storyboard, Submission
from codecast.render.renderer import clip_still, render_all
from codecast.voice.timeline import build_timeline, synthesize_storyboard

from tests.fakes import SilenceEngine
from tests.unit.storyboards import CLIP_FILE, clip_submission

pytestmark = pytest.mark.integration


def make_recording(path: Path) -> Path:
    """2 s at 30 fps, 320x180: red for the first second, blue for the second."""
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=red:s=320x180:r=30:d=1",
                    "-f", "lavfi", "-i", "color=c=blue:s=320x180:r=30:d=1", "-filter_complex",
                    "[0:v][1:v]concat=n=2:v=1[v]", "-map", "[v]", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    str(path)], check=True, capture_output=True)
    return path


def colour_at(mp4: Path, t: float, tmp_path: Path) -> tuple[int, int, int]:
    png = tmp_path / f"at_{t:.2f}.png"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{t:.3f}", "-i", str(mp4), "-frames:v", "1", str(png)],
                   check=True, capture_output=True)
    with Image.open(png) as im:
        return im.convert("RGB").getpixel((im.width // 2, im.height // 2))


def is_red(c):
    return c[0] > 180 and c[2] < 80


def is_blue(c):
    return c[2] > 180 and c[0] < 80


def test_validator_probes_the_real_duration(tiny_repo):
    make_recording(Path(tiny_repo.root) / CLIP_FILE)
    assert video_duration(Path(tiny_repo.root) / CLIP_FILE) == pytest.approx(2.0, abs=0.05)
    ctx = ValidationContext(repomap=tiny_repo, llm={}, output_allowlist=[])
    assert validate_submission(clip_submission(0.5, 2.0), ctx).ok
    res = validate_submission(clip_submission(0.5, 2.5), ctx)
    assert [i.code for i in res.issues] == ["CLIP_RANGE_INVALID"]
    (Path(tiny_repo.root) / "rec" / "fake.mp4").write_text("not a video")
    raw = clip_submission()
    raw["sources"][-1]["file"] = "rec/fake.mp4"
    assert [i.code for i in validate_submission(raw, ctx).issues] == ["VIDEO_UNREADABLE"]


def test_clip_still_is_the_first_frame(tmp_path):
    rec = make_recording(tmp_path / "rec.mp4")
    png = tmp_path / "still.png"
    clip_still(ClipSource(path=str(rec), start=1.2, end=2.0), png)
    with Image.open(png) as im:
        assert im.size == (1920, 1080) and is_blue(im.convert("RGB").getpixel((960, 540)))


def test_render_all_makes_clip_frames(renderer, tiny_repo, tmp_path):
    make_recording(Path(tiny_repo.root) / CLIP_FILE)
    sub = Submission.model_validate(clip_submission(1.0, 2.0))
    sb = Storyboard(repo_url=tiny_repo.repo_url, commit_sha="c", title=sub.title,
                    sources={s.id: s for s in sub.sources}, scenes=sub.scenes)
    tl = synthesize_storyboard(sb, SilenceEngine(0.1), tmp_path / "audio", gap_s=0.15, tail_s=0.5)
    frames = render_all(sb, tl, Path(tiny_repo.root), tmp_path / "frames", renderer)
    clip = frames[4]
    assert clip.motion == "clip" and clip.duration == tl.scenes[4].duration and clip.captions
    assert Path(clip.clip.path) == (Path(tiny_repo.root) / CLIP_FILE).resolve()
    assert (clip.clip.start, clip.clip.end) == (1.0, 2.0)
    with Image.open(clip.frames[0]) as im:
        assert im.size == (1920, 1080) and is_blue(im.convert("RGB").getpixel((960, 400)))
    assert {f.motion for f in frames[:4]} == {"static"}


def test_compose_fits_clips_to_the_narration(tmp_path):
    rec = make_recording(tmp_path / "rec.mp4")
    eng = SilenceEngine(0.5)
    a1, a2 = tmp_path / "a1.wav", tmp_path / "a2.wav"
    eng.synthesize("a", a1)          # 0.5 s + 0.5 s tail -> 1.0 s scene
    eng.synthesize("a b c", a2)      # 1.5 s + 0.5 s tail -> 2.0 s scene
    tl = build_timeline([("scene01", [("a b c", a2, 1.5)]), ("scene02", [("a", a1, 0.5)]),
                         ("scene03", [("a b c", a2, 1.5)])], gap_s=0.15, tail_s=0.5)
    assert [s.duration for s in tl.scenes] == [2.0, 1.0, 2.0]
    still = tmp_path / "s1.png"
    Image.new("RGB", (1920, 1080), (0, 160, 0)).save(still)
    frames = [
        SceneFrames(scene_id="scene01", frames=[str(still)], motion="static", duration=2.0),
        # 2 s of video in a 1 s scene: twice as fast, so red then blue
        SceneFrames(scene_id="scene02", frames=[str(still)], motion="clip", duration=1.0,
                    clip=ClipSource(path=str(rec), start=0.0, end=2.0)),
        # 0.5 s (red, then blue) in a 2 s scene: plays at normal speed, then holds its last (blue) frame
        SceneFrames(scene_id="scene03", frames=[str(still)], motion="clip", duration=2.0,
                    clip=ClipSource(path=str(rec), start=0.75, end=1.25)),
    ]
    out = tmp_path / "explainer.mp4"
    result = compose_video(frames, tl, out, tmp_path / "work", lo=tl.total - 0.1, hi=tl.total + 0.1)
    assert result["duration_s"] == pytest.approx(5.0, abs=0.05)
    for sid, frames_expected in (("scene02", 30), ("scene03", 60)):
        seg = [s for s in probe(tmp_path / "work" / f"{sid}.mp4")["streams"] if s["codec_type"] == "video"][0]
        assert (seg["width"], seg["height"], seg["pix_fmt"], seg["r_frame_rate"]) == (1920, 1080, "yuv420p", "30/1")
        assert float(seg["duration"]) == pytest.approx(frames_expected / 30, abs=0.01)
    assert not [s for s in probe(tmp_path / "work" / "scene02.mp4")["streams"] if s["codec_type"] == "audio"]
    assert colour_at(out, 1.0, tmp_path)[1] > 120                       # scene01 still (green)
    assert is_red(colour_at(out, 2.2, tmp_path))                         # scene02, source 0.4 s
    assert is_blue(colour_at(out, 2.8, tmp_path))                        # scene02, source 1.6 s
    assert is_red(colour_at(out, 3.1, tmp_path))                         # scene03, source 0.85 s
    assert is_blue(colour_at(out, 3.4, tmp_path))                        # scene03, source 1.15 s
    assert is_blue(colour_at(out, 4.9, tmp_path))                        # scene03 held last frame
