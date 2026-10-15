from codecast.grounding.validator import ValidationContext, validate_submission

from tests.unit.storyboards import valid_submission


def ctx(repo):
    return ValidationContext(repomap=repo, llm={"provider": "fake", "model": "m"},
                             output_allowlist=["*.log"])


def codes(result):
    return sorted({i.code for i in result.issues})


def test_valid_submission_accepted(tiny_repo):
    res = validate_submission(valid_submission(), ctx(tiny_repo))
    assert res.issues == []
    assert res.ok
    sb = res.storyboard
    assert sb.repo_url == "https://github.com/o/tiny" and sb.commit_sha == "c0ffee"
    assert set(sb.sources) == {"readme", "code", "models", "f1", "plot"}


def test_non_object_is_schema_error(tiny_repo):
    res = validate_submission("not a dict", ctx(tiny_repo))
    assert codes(res) == ["SCHEMA"] and res.storyboard is None


def test_schema_error_path_is_readable(tiny_repo):
    raw = valid_submission()
    del raw["scenes"][2]["span_id"]
    res = validate_submission(raw, ctx(tiny_repo))
    assert codes(res) == ["SCHEMA"]
    assert res.issues[0].path.startswith("scenes[2]")


def test_ungrounded_number_in_narration(tiny_repo):
    raw = valid_submission()
    raw["scenes"][3]["narration"][0]["text"] = "The large@8 model reaches an F1 score of 0.74 overall."
    res = validate_submission(raw, ctx(tiny_repo))
    bad = [i for i in res.issues if i.code == "UNGROUNDED_NUMBER"]
    assert len(bad) == 1
    assert bad[0].path == "scenes[3].narration[0]"
    assert "0.74" in bad[0].message and "models" in bad[0].message


def test_number_only_grounded_by_cited_source(tiny_repo):
    raw = valid_submission()
    # 0.7349 exists in README, but this sentence cites only the image
    raw["scenes"][4]["narration"][0]["text"] = "This plot shows 0.7349 at a glance for every model here."
    res = validate_submission(raw, ctx(tiny_repo))
    assert "UNGROUNDED_NUMBER" in codes(res)


def test_ungrounded_identifier_on_screen(tiny_repo):
    raw = valid_submission()
    raw["scenes"][1]["items"][0] = "Uses ResNet50"
    res = validate_submission(raw, ctx(tiny_repo))
    bad = [i for i in res.issues if i.code == "UNGROUNDED_IDENTIFIER"]
    assert [i.path for i in bad] == ["scenes[1].items[0]"]


def test_number_word_is_checked(tiny_repo):
    raw = valid_submission()
    raw["scenes"][0]["narration"][1]["text"] = "It ships seven tools and keeps the pipeline easy to run."
    res = validate_submission(raw, ctx(tiny_repo))
    assert any(i.code == "UNGROUNDED_NUMBER" and "7" in i.message for i in res.issues)


def test_mermaid_source_is_on_screen_text(tiny_repo):
    raw = valid_submission()
    raw["scenes"][4] = {"type": "diagram", "mermaid": "flowchart LR\n  A[Clip] --> B1[Score]",
                        "narration": raw["scenes"][4]["narration"]}
    raw["scenes"][4]["narration"][0]["source_ids"] = ["readme"]
    res = validate_submission(raw, ctx(tiny_repo))
    assert any(i.code == "UNGROUNDED_IDENTIFIER" and "B1" in i.message for i in res.issues)


def test_unknown_and_wrong_kind_references(tiny_repo):
    raw = valid_submission()
    raw["scenes"][2]["span_id"] = "plot"
    raw["scenes"][0]["narration"][0]["source_ids"] = ["ghost"]
    res = validate_submission(raw, ctx(tiny_repo))
    assert {"WRONG_SOURCE_KIND", "UNKNOWN_SOURCE_ID"} <= set(codes(res))


def test_all_errors_collected_together(tiny_repo):
    raw = valid_submission()
    raw["sources"][0]["text"] = "made up"
    raw["scenes"][1]["items"][0] = "Uses ResNet50"
    res = validate_submission(raw, ctx(tiny_repo))
    assert {"SPAN_TEXT_MISMATCH", "UNGROUNDED_IDENTIFIER"} <= set(codes(res))
