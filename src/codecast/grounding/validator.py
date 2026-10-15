"""Storyboard validator (spec §7). Pure: no I/O beyond reading cited repo files."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from pydantic import ValidationError

from codecast.grounding.numbers import (
    extract_claims, identifier_grounded, number_grounded, source_number_literals,
)
from codecast.grounding.spans import SourceIndex, check_sources
from codecast.models import (
    BulletsScene, CellRef, CodeScene, DiagramScene, ImageScene, Issue, MetricsTableScene,
    OutroScene, RepoMap, Storyboard, Submission, TerminalScene, TitleScene,
)


@dataclass
class ValidationContext:
    repomap: RepoMap
    llm: dict[str, str]
    output_allowlist: list[str]
    mermaid_check: Callable[[str], str | None] | None = None


@dataclass
class ValidationResult:
    storyboard: Storyboard | None
    issues: list[Issue]

    @property
    def ok(self) -> bool:
        return self.storyboard is not None and not self.issues


def _loc(loc: tuple) -> str:
    out = ""
    for part in loc:
        out += f"[{part}]" if isinstance(part, int) else (f".{part}" if out else str(part))
    return out or "storyboard"


def scene_source_ids(scene) -> set[str]:
    ids = {sid for s in scene.narration for sid in s.source_ids}
    if isinstance(scene, CodeScene):
        ids.add(scene.span_id)
    elif isinstance(scene, ImageScene):
        ids.add(scene.image_id)
    elif isinstance(scene, MetricsTableScene):
        ids |= {c.span_id for row in scene.rows for c in row if isinstance(c, CellRef)}
    elif isinstance(scene, TerminalScene):
        ids |= set(scene.command_span_ids)
        if scene.output_span_id:
            ids.add(scene.output_span_id)
    return ids


def on_screen_texts(scene) -> list[tuple[str, str]]:
    if isinstance(scene, TitleScene):
        out = [("heading", scene.heading)]
        if scene.subheading:
            out.append(("subheading", scene.subheading))
        return out
    if isinstance(scene, BulletsScene):
        return [("heading", scene.heading)] + [(f"items[{k}]", t) for k, t in enumerate(scene.items)]
    if isinstance(scene, MetricsTableScene):
        out = [(f"columns[{k}]", t) for k, t in enumerate(scene.columns)]
        out += [(f"rows[{r}][{c}]", cell) for r, row in enumerate(scene.rows)
                for c, cell in enumerate(row) if isinstance(cell, str)]
        return out
    if isinstance(scene, DiagramScene):
        return [("mermaid", scene.mermaid)]
    if isinstance(scene, OutroScene):
        return [("cta", scene.cta)]
    return []


_EXPECTED_KIND = {"span_id": "text", "image_id": "image", "cell": "json", "command": "text", "output": "text"}


def _reference_issues(sb: Storyboard, index: SourceIndex) -> list[Issue]:
    issues: list[Issue] = []

    def need(sid: str, path: str, kind: str | None) -> None:
        span = sb.sources.get(sid)
        if span is None:
            issues.append(Issue(path=path, code="UNKNOWN_SOURCE_ID", message=f"source {sid!r} is not declared"))
        elif kind and span.kind != kind:
            issues.append(Issue(path=path, code="WRONG_SOURCE_KIND",
                                message=f"source {sid!r} is {span.kind}, expected {kind}"))

    for i, sc in enumerate(sb.scenes):
        for j, sent in enumerate(sc.narration):
            for sid in sent.source_ids:
                need(sid, f"scenes[{i}].narration[{j}]", None)
        if isinstance(sc, CodeScene):
            need(sc.span_id, f"scenes[{i}].span_id", "text")
        elif isinstance(sc, ImageScene):
            need(sc.image_id, f"scenes[{i}].image_id", "image")
        elif isinstance(sc, MetricsTableScene):
            for r, row in enumerate(sc.rows):
                for c, cell in enumerate(row):
                    if isinstance(cell, CellRef):
                        need(cell.span_id, f"scenes[{i}].rows[{r}][{c}]", "json")
        elif isinstance(sc, TerminalScene):
            for k, sid in enumerate(sc.command_span_ids):
                need(sid, f"scenes[{i}].command_span_ids[{k}]", "text")
            if sc.output_span_id:
                need(sc.output_span_id, f"scenes[{i}].output_span_id", "text")
    return issues


def _ground(text: str, ids: list[str], index: SourceIndex, path: str) -> list[Issue]:
    texts = [index.texts[s] for s in ids if s in index.texts]
    literals = [lit for t in texts for lit in source_number_literals(t)]
    issues, seen = [], set()
    for claim in extract_claims(text):
        if claim.token in seen:
            continue
        seen.add(claim.token)
        if claim.kind == "number" and not number_grounded(claim.token, literals):
            issues.append(Issue(path=path, code="UNGROUNDED_NUMBER",
                                message=f"{claim.token} not found in cited sources {ids}"))
        elif claim.kind == "identifier" and not identifier_grounded(claim.token, texts):
            issues.append(Issue(path=path, code="UNGROUNDED_IDENTIFIER",
                                message=f"{claim.token} not found (case-sensitive, whole token) "
                                        f"in cited sources {ids}"))
    return issues


def _grounding_issues(sb: Storyboard, index: SourceIndex) -> list[Issue]:
    issues: list[Issue] = []
    for i, sc in enumerate(sb.scenes):
        for j, sent in enumerate(sc.narration):
            issues += _ground(sent.text, sent.source_ids, index, f"scenes[{i}].narration[{j}]")
        scene_ids = sorted(scene_source_ids(sc))
        for suffix, text in on_screen_texts(sc):
            issues += _ground(text, scene_ids, index, f"scenes[{i}].{suffix}")
    return issues


def structure_issues(sb: Storyboard) -> list[Issue]:
    return []


def scene_rule_issues(sb: Storyboard, index: SourceIndex, ctx: ValidationContext) -> list[Issue]:
    return []


def validate_submission(raw: Any, ctx: ValidationContext) -> ValidationResult:
    if not isinstance(raw, dict):
        return ValidationResult(None, [Issue(path="storyboard", code="SCHEMA",
                                             message="storyboard must be a JSON object")])
    try:
        sub = Submission.model_validate(raw)
    except ValidationError as e:
        return ValidationResult(None, [Issue(path=_loc(err["loc"]), code="SCHEMA", message=err["msg"])
                                       for err in e.errors()[:20]])
    issues, index = check_sources(sub.sources, ctx.repomap)
    sb = Storyboard(repo_url=ctx.repomap.repo_url, commit_sha=ctx.repomap.commit_sha, title=sub.title,
                    sources={s.id: s for s in sub.sources}, scenes=sub.scenes, llm=ctx.llm)
    issues += _reference_issues(sb, index)
    issues += _grounding_issues(sb, index)
    issues += structure_issues(sb)
    issues += scene_rule_issues(sb, index, ctx)
    return ValidationResult(None if issues else sb, issues)
