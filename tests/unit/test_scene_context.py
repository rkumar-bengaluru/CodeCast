from pathlib import Path

from codecast.models import Storyboard, Submission
from codecast.render.renderer import STATIC_URL, _env, highlight_code, scene_context

from tests.unit.storyboards import valid_submission


def sb_for(repo):
    sub = Submission.model_validate(valid_submission())
    return Storyboard(repo_url=repo.repo_url, commit_sha="c", title=sub.title,
                      sources={s.id: s for s in sub.sources}, scenes=sub.scenes)


def test_code_highlight_uses_real_line_numbers():
    from codecast.models import TextSpan
    span = TextSpan(id="s", file="src/app.py", start_line=41, end_line=42, text='def main():\n    print("hello")')
    html, css = highlight_code(span, [42])
    assert ">41<" in html and ">42<" in html and 'class="hll"' in html
    assert ".hl" in css


def test_contexts_for_each_type(tiny_repo):
    sb = sb_for(tiny_repo)
    root = Path(tiny_repo.root)
    names = [scene_context(sb, i, root)[0] for i in range(len(sb.scenes))]
    assert names == ["title.html", "bullets.html", "code.html", "metrics_table.html", "image.html", "outro.html"]
    _, table = scene_context(sb, 3, root)
    assert table["rows"] == [[{"text": "large@8", "num": False}, {"text": "0.735", "num": True}]]
    _, image = scene_context(sb, 4, root)
    assert image["src"] == "http://codecast.local/repo/plot.png" and image["highlights"] == [[0.1, 0.1, 0.3, 0.3]]
    assert image["static"] == STATIC_URL


def test_terminal_context_strips_prompt(tiny_repo):
    raw = valid_submission()
    raw["sources"].append({"kind": "text", "id": "cmd", "file": "README.md", "start_line": 3, "end_line": 3,
                           "text": "Run `python app.py --top 3` to predict."})
    raw["scenes"][4] = {"type": "terminal", "command_span_ids": ["cmd"],
                        "narration": [{"text": "Run it.", "source_ids": ["cmd"]}]}
    sub = Submission.model_validate(raw)
    sb = Storyboard(repo_url="u", commit_sha="c", title="t", sources={s.id: s for s in sub.sources}, scenes=sub.scenes)
    name, ctx = scene_context(sb, 4, Path(tiny_repo.root))
    assert name == "terminal.html" and ctx["commands"] == ["Run `python app.py --top 3` to predict."]


def test_autoescape_review_focus_3():
    html = _env.get_template("bullets.html").render(static=STATIC_URL, kicker="k", scene_number=1, scene_count=2,
                                                    heading='<script>alert("x")</script> & co', items=["a < b"])
    assert "<script>alert" not in html
    assert "&lt;script&gt;" in html and "&amp; co" in html and "a &lt; b" in html
