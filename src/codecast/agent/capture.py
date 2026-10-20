"""Exact LLM traffic for failure review: workspace/<run>/llm/{system_prompt.md, step-NNN.request|response.json}."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


_DATA_URL = re.compile(r"^data:[\w/+.-]+;base64,")


def _omit_images(obj: Any) -> Any:
    """Copy of a request body with base64 image payloads replaced by their size (each step resends them)."""
    if isinstance(obj, dict):
        if obj.get("type") == "base64" and isinstance(obj.get("data"), str):
            return {**obj, "data": f"<{len(obj['data'])} chars of base64 omitted>"}
        return {k: _omit_images(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_omit_images(v) for v in obj]
    if isinstance(obj, str) and _DATA_URL.match(obj):
        head, _, data = obj.partition(";base64,")
        return f"{head};base64,<{len(data)} chars of base64 omitted>"
    return obj


class LLMCapture:
    def __init__(self, dir: Path):
        self.dir = Path(dir)
        self.last_request_path: Path | None = None
        self._prompt_written = False

    def _write(self, name: str, text: str) -> Path:
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self.dir / name
        path.write_text(text, encoding="utf-8")
        return path

    def system_prompt(self, text: str) -> None:
        if not self._prompt_written:
            self._write("system_prompt.md", text)
            self._prompt_written = True

    def request(self, step: int, body: dict[str, Any] | None) -> Path | None:
        """The provider's wire body as sent; None when the provider exposes none."""
        self.last_request_path = None if body is None else self._write(
            f"step-{step:03d}.request.json", json.dumps(_omit_images(body), indent=1, ensure_ascii=False, default=str))
        return self.last_request_path

    def response(self, step: int, data: dict[str, Any]) -> Path:
        return self._write(f"step-{step:03d}.response.json", json.dumps(data, indent=1, ensure_ascii=False, default=str))
