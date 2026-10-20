"""Exact LLM traffic for failure review: workspace/<run>/llm/{system_prompt.md, step-NNN.request|response.json}."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


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
            f"step-{step:03d}.request.json", json.dumps(body, indent=1, ensure_ascii=False, default=str))
        return self.last_request_path

    def response(self, step: int, data: dict[str, Any]) -> Path:
        return self._write(f"step-{step:03d}.response.json", json.dumps(data, indent=1, ensure_ascii=False, default=str))
