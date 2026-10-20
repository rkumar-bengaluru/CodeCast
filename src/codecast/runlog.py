"""Per-run log file: everything under the `codecast` logger goes to workspace/<run>/run.log."""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Callable

FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
FILE_ONLY = {"file_only": True}  # extra= for records the CLI already reports in its own words


class _ConsoleFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not getattr(record, "file_only", False)


def attach(ws: Path, *, verbose: bool) -> Callable[[], None]:
    """Log DEBUG and up to ws/run.log and INFO (verbose) or WARNING to stderr; returns the detach function."""
    Path(ws).mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("codecast")
    logger.setLevel(logging.DEBUG)
    file_handler = logging.FileHandler(Path(ws) / "run.log", mode="a", encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    console = logging.StreamHandler(sys.stderr)
    console.setLevel(logging.INFO if verbose else logging.WARNING)
    console.addFilter(_ConsoleFilter())  # tracebacks stay in run.log; the terminal gets the readable summary
    for h in (file_handler, console):
        h.setFormatter(logging.Formatter(FORMAT))
        logger.addHandler(h)

    def detach() -> None:
        for h in (file_handler, console):
            logger.removeHandler(h)
            h.close()
    return detach
