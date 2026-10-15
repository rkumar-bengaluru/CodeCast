"""Wraps validate_submission with round counting (spec §7: max 4 rounds → exit 5)."""

from __future__ import annotations

import json
from collections import Counter
from typing import Any

from codecast.agent.tools import SubmitOutcome
from codecast.errors import ValidationFailed
from codecast.grounding.validator import ValidationContext, validate_submission
from codecast.models import Storyboard

MAX_FEEDBACK_ERRORS = 60


class Submitter:
    def __init__(self, ctx: ValidationContext, *, max_rounds: int = 4):
        self.ctx = ctx
        self.max_rounds = max_rounds
        self.rounds_total = 0
        self.rounds_phase = 0
        self.failures_by_code: Counter[str] = Counter()
        self.accepted: Storyboard | None = None

    def begin_revision(self) -> None:
        self.rounds_phase = 0

    def __call__(self, raw: Any) -> SubmitOutcome:
        self.rounds_total += 1
        self.rounds_phase += 1
        res = validate_submission(raw, self.ctx)
        if res.ok:
            self.accepted = res.storyboard
            return SubmitOutcome(True, res.storyboard, "accepted")
        self.failures_by_code.update(i.code for i in res.issues)
        errors = [i.model_dump() for i in res.issues[:MAX_FEEDBACK_ERRORS]]
        if self.rounds_phase >= self.max_rounds:
            raise ValidationFailed(f"storyboard rejected {self.rounds_phase} times in a row",
                                   details={"issues": errors})
        return SubmitOutcome(False, None, json.dumps(
            {"accepted": False, "rounds_left": self.max_rounds - self.rounds_phase,
             "n_errors": len(res.issues), "errors": errors}, indent=1, ensure_ascii=False))
