# Title kicker and hideable link: implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A storyboard can set the title slides' kicker line and hide the repo link on title and outro slides. A
storyboard that sets neither renders exactly as today.

**Architecture:** There are two new top-level `Storyboard` fields with today's values as defaults. `scene_context`
passes them to `title.html` and `outro.html`. The scene models and the LLM's `AgentSubmission` schema are not
touched.

**Tech Stack:** Python 3.12, pydantic v2, Jinja2 templates, pytest.

**Spec:** `~/Documents/2026/rkumar-bengaluru/Walker/docs/design/specs/2026-11-01-desktop-take-design.md` §5.1 and §7
(decisions in `Walker/docs/design/decisions.md`).

## Global Constraints

- Backward compatibility is of utmost importance: a storyboard JSON without the new fields loads and renders the
  same HTML as before.
- `Storyboard.version` stays 1. `TitleScene`, `OutroScene`, `Submission` and `AgentSubmission` are unchanged.
- Field names: `title_kicker: str = "Repository tour"` and `show_url: bool = True`. The name `kicker` is already
  taken: `scene_context`'s base dict passes `kicker = sb.title` to the other slides.
- An empty `title_kicker` (`""`) hides the title slide's kicker line.
- No CodeCast CLI changes.
- Tests: `.venv/bin/python -m pytest -q tests/unit`. Lint: `uvx ruff@0.15.0 check src tests` if the repo has ruff config, else skip.

## Review Focus

1. An old `storyboard.json` (written before this change) passed to `--from-stage render` must validate under
   `extra="forbid"`. This is pinned by the test that validates a dict without the new keys.
2. A kicker with HTML characters (`<b>&`) must render escaped, not as markup. Jinja autoescape: test it.
3. With `show_url=False`, the outro must still show its cta and title.

---

### Task 1: Storyboard fields, templates, renderer

**Files:**
- Modify: `src/codecast/models.py` (`Storyboard`)
- Modify: `src/codecast/render/renderer.py:78` and `:101` (`scene_context`)
- Modify: `src/codecast/render/templates/title.html`, `outro.html`
- Test: `tests/unit/test_scene_context.py`

**Interfaces:**
- Produces: `Storyboard.title_kicker: str = "Repository tour"` and `Storyboard.show_url: bool = True`. Walker sets
  these on the storyboard it builds.
- Produces: `scene_context` adds `title_kicker` to the title context and `show_url` to the title and outro contexts.

- [ ] **Step 1: Write the failing tests** in `tests/unit/test_scene_context.py`. Reuse `sb_for(tiny_repo)`, and
  render through `_env().get_template(name).render(**ctx)`.

```python
def _html(sb, i, root):
    name, ctx = scene_context(sb, i, root)
    return _env().get_template(name).render(**ctx)

def test_defaults_render_as_before(tiny_repo):
    sb, root = sb_for(tiny_repo), Path(tiny_repo.root)
    title, outro = _html(sb, 0, root), _html(sb, len(sb.scenes) - 1, root)
    assert '<div class="kicker">Repository tour</div>' in title
    assert f'<div class="url">{sb.repo_url}</div>' in title
    assert f'<div class="url">{sb.repo_url}</div>' in outro

def test_old_storyboard_json_still_loads(tiny_repo):
    raw = sb_for(tiny_repo).model_dump(mode="json")
    raw.pop("title_kicker"); raw.pop("show_url")
    sb = Storyboard.model_validate(raw)
    assert sb.title_kicker == "Repository tour" and sb.show_url is True

def test_custom_kicker_and_no_link(tiny_repo):
    sb = sb_for(tiny_repo).model_copy(update={"title_kicker": "Investor preview", "show_url": False})
    root = Path(tiny_repo.root)
    title, outro = _html(sb, 0, root), _html(sb, len(sb.scenes) - 1, root)
    assert '<div class="kicker">Investor preview</div>' in title and "Repository tour" not in title
    assert 'class="url"' not in title and 'class="url"' not in outro
    assert sb.title in outro   # the outro's kicker (the video title) and cta stay

def test_empty_kicker_hides_the_line(tiny_repo):
    sb = sb_for(tiny_repo).model_copy(update={"title_kicker": ""})
    assert 'class="kicker"' not in _html(sb, 0, Path(tiny_repo.root))

def test_kicker_is_escaped(tiny_repo):
    sb = sb_for(tiny_repo).model_copy(update={"title_kicker": "<b>&"})
    assert "&lt;b&gt;&amp;" in _html(sb, 0, Path(tiny_repo.root))

def test_llm_schema_unchanged():
    from codecast.models import AgentSubmission
    props = AgentSubmission.model_json_schema()
    assert "title_kicker" not in str(props) and "show_url" not in str(props)
```

- [ ] **Step 2: Run them; they fail.** Run `.venv/bin/python -m pytest -q tests/unit/test_scene_context.py`. Expected:
  the custom, empty, escaped and old-JSON tests FAIL (unknown field / no key). The defaults and schema tests may
  already pass; that is fine.
- [ ] **Step 3: Implement.**
  - `Storyboard` gets the two fields after `llm`.
  - `title.html`: `{% if title_kicker %}<div class="kicker">{{ title_kicker }}</div>{% endif %}`.
  - Both templates wrap the url div in `{% if show_url %}…{% endif %}`.
  - `scene_context` passes `title_kicker=sb.title_kicker, show_url=sb.show_url` for title, and `show_url=sb.show_url`
    for outro.
  - Keep the templates' whitespace and indentation, so default HTML is unchanged.
- [ ] **Step 4: Run the whole unit suite.** Run `.venv/bin/python -m pytest -q tests/unit`. Expected: all pass,
  including the existing `test_contexts_for_each_type`.
- [ ] **Step 5: Commit.** `git commit -m "feat: storyboard title_kicker and show_url (defaults unchanged)"`.

### Task 2: Docs

**Files:**
- Modify: `README.md` (the paragraph on tools that write their own storyboard)

- [ ] **Step 1:** Add one sentence to that paragraph: such tools may also set `title_kicker` (default "Repository
  tour"; `""` hides it) and `show_url` (default true) on the storyboard.
- [ ] **Step 2: Commit, push the branch, and open a PR.** The PR body asks the maintainer to merge and tag `v1.1.0`.
