"""Data contracts (spec §6) plus RepoMap (§3) and render artifacts (§9.1)."""

from __future__ import annotations

from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field

# Allowed length of the final video in seconds, enforced by the voice loop, the ffprobe post-check and eval.


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---- sources (§6.1) ----
class TextSpan(_Strict):
    kind: Literal["text"] = "text"
    id: str
    file: str
    start_line: int
    end_line: int
    text: str | None = None


class JsonSpan(_Strict):
    kind: Literal["json"] = "json"
    id: str
    file: str
    pointer: str
    value: Any = None  # omitted (see model_fields_set) means "tool fills it"


class ImageRef(_Strict):
    kind: Literal["image"] = "image"
    id: str
    file: str


class VideoRef(_Strict):
    """A screen recording in the checkout (.mp4/.webm), played by a clip scene. It grounds no claims."""
    kind: Literal["video"] = "video"
    id: str
    file: str


Source = Annotated[Union[TextSpan, JsonSpan, ImageRef, VideoRef], Field(discriminator="kind")]


# ---- scenes (§6.2) ----
class Sentence(_Strict):
    text: str
    source_ids: list[str]


Rect = tuple[float, float, float, float]


class StaticMotion(_Strict):
    kind: Literal["static"] = "static"


class KenBurnsMotion(_Strict):
    kind: Literal["ken_burns"] = "ken_burns"
    from_rect: Rect
    to_rect: Rect


class _SceneBase(_Strict):
    narration: list[Sentence]


class TitleScene(_SceneBase):
    type: Literal["title"] = "title"
    heading: str
    subheading: str | None = None


class BulletsScene(_SceneBase):
    type: Literal["bullets"] = "bullets"
    heading: str
    items: list[str]


class CodeScene(_SceneBase):
    type: Literal["code"] = "code"
    span_id: str
    highlight_lines: list[int] = []


class ImageScene(_SceneBase):
    type: Literal["image"] = "image"
    image_id: str
    highlights: list[Rect] = []
    motion: Annotated[Union[StaticMotion, KenBurnsMotion], Field(discriminator="kind")] = StaticMotion()


class CellRef(_Strict):
    span_id: str
    decimals: int | None = None


class MetricsTableScene(_SceneBase):
    type: Literal["metrics_table"] = "metrics_table"
    columns: list[str]
    rows: list[list[Union[str, CellRef]]]


class DiagramScene(_SceneBase):
    type: Literal["diagram"] = "diagram"
    mermaid: str


class TerminalScene(_SceneBase):
    type: Literal["terminal"] = "terminal"
    command_span_ids: list[str]
    output_span_id: str | None = None


class ClipScene(_SceneBase):
    """Plays [start_s, end_s] of a video source, fitted to the narration length at compose time."""
    type: Literal["clip"] = "clip"
    video_id: str
    start_s: float
    end_s: float


class OutroScene(_SceneBase):
    type: Literal["outro"] = "outro"
    repo_url: str
    cta: str


Scene = Annotated[
    Union[TitleScene, BulletsScene, CodeScene, ImageScene, MetricsTableScene, DiagramScene,
          TerminalScene, ClipScene, OutroScene],
    Field(discriminator="type"),
]


def scene_id(index: int) -> str:
    return f"scene{index + 1:02d}"


# ---- storyboard (§6.3) ----
class Submission(_Strict):
    title: str
    sources: list[Source]
    scenes: list[Scene]


# What the LLM director may submit: the catalog without clip scenes and video sources. Those exist for tools
# that write their own storyboard from screen recordings; offering them to the LLM would only cost it rounds.
AgentSource = Annotated[Union[TextSpan, JsonSpan, ImageRef], Field(discriminator="kind")]
AgentScene = Annotated[
    Union[TitleScene, BulletsScene, CodeScene, ImageScene, MetricsTableScene, DiagramScene,
          TerminalScene, OutroScene],
    Field(discriminator="type"),
]


class AgentSubmission(_Strict):
    model_config = ConfigDict(extra="forbid", title="Submission")  # same tool schema the LLM saw before clips
    title: str
    sources: list[AgentSource]
    scenes: list[AgentScene]


class Storyboard(_Strict):
    version: int = 1
    repo_url: str
    commit_sha: str
    title: str
    sources: dict[str, Source]
    scenes: list[Scene]
    llm: dict[str, str] = {}

    def to_submission(self) -> dict:
        return {
            "title": self.title,
            "sources": [s.model_dump(mode="json") for s in self.sources.values()],
            "scenes": [s.model_dump(mode="json") for s in self.scenes],
        }


class Issue(_Strict):
    path: str
    code: str
    message: str


# ---- repo map (§3) ----
class FileEntry(BaseModel):
    path: str
    size: int
    kind: Literal["text", "image", "binary"]


class SkippedEntry(BaseModel):
    path: str
    reason: Literal["ignored_dir", "too_large", "symlink_escape", "gitignored", "lfs_pointer", "binary"]


class RepoMap(BaseModel):
    repo_url: str
    commit_sha: str
    root: str
    n_files: int
    files: list[FileEntry]
    skipped: list[SkippedEntry]
    tree_text: str

    def file(self, path: str) -> FileEntry | None:
        for f in self.files:
            if f.path == path:
                return f
        return None


# ---- render artifacts (§9.1) ----
class Box(BaseModel):
    x: float
    y: float
    w: float
    h: float


class CaptionImage(BaseModel):
    path: str
    start: float
    end: float


class ClipSource(BaseModel):
    path: str  # absolute path of the video inside the checkout
    start: float
    end: float


class SceneFrames(BaseModel):
    scene_id: str
    frames: list[str]  # for a clip: its first frame, as a preview still
    motion: Literal["static", "ken_burns", "sequence", "clip"]
    duration: float
    image_box: Box | None = None
    ken_burns: KenBurnsMotion | None = None
    clip: ClipSource | None = None
    captions: list[CaptionImage] = []
