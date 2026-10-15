"""Source integrity (spec §7 step 2): JSON Pointer, exact span text, decimal-safe JSON."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation, localcontext
from pathlib import Path
from typing import Any

from codecast.models import ImageRef, Issue, JsonSpan, RepoMap, Source, TextSpan


class PointerError(Exception):
    pass


def resolve_pointer(doc: Any, pointer: str) -> Any:
    if pointer == "":
        return doc
    if not pointer.startswith("/"):
        raise PointerError("pointer must be empty or start with '/'")
    cur = doc
    for raw in pointer[1:].split("/"):
        tok = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(cur, dict):
            if tok not in cur:
                raise PointerError(f"key {tok!r} not found")
            cur = cur[tok]
        elif isinstance(cur, list):
            # Bounded digits keep int() far below the int-string conversion limit.
            if not re.fullmatch(r"0|[1-9]\d{0,17}", tok) or int(tok) >= len(cur):
                raise PointerError(f"invalid array index {tok!r}")
            cur = cur[int(tok)]
        else:
            raise PointerError(f"cannot descend into {type(cur).__name__} at {tok!r}")
    return cur


def read_text_normalized(path: Path) -> str:
    text = Path(path).read_bytes().decode("utf-8", errors="replace")
    return text[1:] if text.startswith("﻿") else text


def read_lines(path: Path) -> list[str]:
    return read_text_normalized(path).splitlines()


def load_json_decimal(path: Path) -> Any:
    return json.loads(read_text_normalized(path), parse_float=Decimal)


def dumps_decimal(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (int, Decimal)):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return "[" + ", ".join(dumps_decimal(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{json.dumps(str(k), ensure_ascii=False)}: {dumps_decimal(v)}"
                               for k, v in value.items()) + "}"
    return json.dumps(str(value))


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float, Decimal)) and not isinstance(v, bool)


def _num_equal(a: Any, b: Any) -> bool:
    if isinstance(a, int) and isinstance(b, int):
        return a == b
    try:
        return Decimal(str(a)) == Decimal(str(b))
    except (ValueError, InvalidOperation):
        return False


def _show(value: Any) -> str:
    try:
        return dumps_decimal(value)
    except (RecursionError, ValueError):
        return "<value too large to show>"


def values_equal(agent_value: Any, resolved: Any) -> bool:
    if _is_number(resolved):
        return _is_number(agent_value) and _num_equal(agent_value, resolved)
    if isinstance(resolved, list):
        return (isinstance(agent_value, list) and len(agent_value) == len(resolved)
                and all(values_equal(a, r) for a, r in zip(agent_value, resolved)))
    if isinstance(resolved, dict):
        return (isinstance(agent_value, dict) and agent_value.keys() == resolved.keys()
                and all(values_equal(agent_value[k], resolved[k]) for k in resolved))
    return type(agent_value) is type(resolved) and agent_value == resolved


@dataclass
class SourceIndex:
    spans: dict[str, Source] = field(default_factory=dict)
    texts: dict[str, str] = field(default_factory=dict)
    values: dict[str, Any] = field(default_factory=dict)


def _issue(i: int, code: str, message: str) -> Issue:
    return Issue(path=f"sources[{i}]", code=code, message=message)


def _mismatch_hint(claimed: str, actual: str, start_line: int) -> str:
    """Say where claimed span text first departs from the file, so the agent can fix it in one round."""
    got, want = claimed.split("\n"), actual.split("\n")
    if len(got) != len(want):
        return (f"your text has {len(got)} line(s); lines {start_line}-{start_line + len(want) - 1} "
                f"are {len(want)} lines")
    k = next(k for k, (a, b) in enumerate(zip(got, want)) if a != b)
    shown = want[k] if len(want[k]) <= 160 else want[k][:160] + "..."
    return f"first difference at line {start_line + k}: the file has {shown!r}, keep its leading whitespace"


def check_sources(sources: list[Source], repomap: RepoMap) -> tuple[list[Issue], SourceIndex]:
    root = Path(repomap.root)
    issues: list[Issue] = []
    index = SourceIndex()
    for i, src in enumerate(sources):
        if src.id in index.spans:
            issues.append(_issue(i, "DUPLICATE_SOURCE_ID", f"source id {src.id!r} declared twice"))
            continue
        index.spans[src.id] = src
        entry = repomap.file(src.file)
        if isinstance(src, ImageRef):
            if entry is None or entry.kind != "image":
                issues.append(_issue(i, "IMAGE_NOT_FOUND", f"{src.file} is not an image in the repo"))
            index.texts[src.id] = ""
            continue
        if entry is None:
            issues.append(_issue(i, "SPAN_FILE_MISSING", f"{src.file} is not a readable file in the repo"))
            continue
        if entry.kind != "text":
            issues.append(_issue(i, "SPAN_NOT_TEXT", f"{src.file} is {entry.kind}, not text"))
            continue
        path = root / src.file
        if isinstance(src, TextSpan):
            try:
                lines = read_lines(path)
            except OSError as e:
                issues.append(_issue(i, "SPAN_FILE_MISSING", f"{src.file} could not be read: {e}"))
                continue
            if not (1 <= src.start_line <= src.end_line <= len(lines)):
                issues.append(_issue(i, "SPAN_RANGE_INVALID",
                                     f"lines {src.start_line}-{src.end_line} outside 1-{len(lines)}"))
                continue
            actual = "\n".join(lines[src.start_line - 1:src.end_line])
            claimed = src.text.replace("\r\n", "\n")
            if claimed.endswith("\n"):
                claimed = claimed[:-1]
            if claimed != actual:
                issues.append(_issue(i, "SPAN_TEXT_MISMATCH",
                                     f"text differs from {src.file}:{src.start_line}-{src.end_line} "
                                     f"({_mismatch_hint(claimed, actual, src.start_line)}); copy the lines "
                                     f"exactly as read_file shows them (without line numbers)"))
                continue
            index.texts[src.id] = actual
        elif isinstance(src, JsonSpan):
            try:
                doc = load_json_decimal(path)
            except (ValueError, UnicodeDecodeError, RecursionError, OSError) as e:
                issues.append(_issue(i, "JSON_PARSE_ERROR", f"{src.file} could not be parsed as JSON: {e}"))
                continue
            try:
                resolved = resolve_pointer(doc, src.pointer)
            except PointerError as e:
                issues.append(_issue(i, "JSON_POINTER_UNRESOLVED", f"{src.pointer}: {e}"))
                continue
            try:
                equal = values_equal(src.value, resolved)
            except RecursionError:
                equal = False
            if not equal:
                issues.append(_issue(i, "JSON_VALUE_MISMATCH",
                                     f"value at {src.pointer} is {_show(resolved)}"))
                continue
            index.values[src.id] = resolved
            index.texts[src.id] = resolved if isinstance(resolved, str) else _show(resolved)
    return issues, index


def format_cell(value: Any, decimals: int | None) -> str | None:
    if not (isinstance(value, (int, Decimal)) and not isinstance(value, bool)):
        return None
    if decimals is not None and decimals < 0:
        return None
    d = Decimal(value)  # Decimal(int) has no digit limit, unlike str(int)
    if not d.is_finite():
        return None
    if decimals is None:
        try:
            return str(value)
        except ValueError:  # int beyond the int-string conversion limit
            return None
    with localcontext() as ctx:
        # Size precision to the operand so wide values cannot raise InvalidOperation.
        ctx.prec = max(28, d.adjusted() + 1 + decimals + 10)
        return f"{d.quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_UP):f}"
