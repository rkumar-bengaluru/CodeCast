import json

import pytest

from codecast.agent.submit import Submitter
from codecast.errors import ValidationFailed
from codecast.grounding.validator import ValidationContext

from tests.unit.storyboards import valid_submission


def submitter(repo):
    return Submitter(ValidationContext(repomap=repo, llm={"provider": "fake", "model": "m"}, output_allowlist=["*.log"]))


def bad():
    raw = valid_submission()
    raw["scenes"][3]["narration"][0]["text"] = "The large@8 model reaches an F1 score of 0.74 overall."
    return raw


def test_accepts_valid(tiny_repo):
    s = submitter(tiny_repo)
    out = s(valid_submission())
    assert out.accepted and out.feedback == "accepted" and s.accepted is out.storyboard


def test_rejection_feedback_lists_errors_and_rounds_left(tiny_repo):
    s = submitter(tiny_repo)
    out = s(bad())
    data = json.loads(out.feedback)
    assert not out.accepted and data["rounds_left"] == 3
    assert data["errors"][0]["code"] == "UNGROUNDED_NUMBER"
    assert s.failures_by_code["UNGROUNDED_NUMBER"] == 1


def test_fourth_rejection_raises(tiny_repo):
    s = submitter(tiny_repo)
    for _ in range(3):
        s(bad())
    with pytest.raises(ValidationFailed) as ei:
        s(bad())
    assert ei.value.exit_code == 5 and ei.value.details["issues"]


def test_revision_gets_fresh_rounds_but_total_counts(tiny_repo):
    s = submitter(tiny_repo)
    s(bad()); s(bad()); s(valid_submission())
    s.begin_revision()
    s(bad()); s(bad()); s(bad())
    assert s(valid_submission()).accepted
    assert s.rounds_total == 7
