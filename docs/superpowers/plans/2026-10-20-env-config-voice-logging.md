# Env-driven limits, custom voice, and failure diagnostics — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the user set duration/word limits and a custom piper voice without touching code, fix the crash that killed run `20261020T114621Z-9ce380`, and make every failed run explain itself through a readable error, a `run.log`, and the exact LLM requests that were sent.

**Architecture:** A `.env` file (loaded at CLI start, never overriding real environment variables) feeds a new `limits` section of `Config`; every place that today imports `DURATION_RANGE_S` / `MIN_WORDS` / `MAX_WORDS` / `TARGET_WORDS` reads `cfg.limits` instead, and the director prompt is rendered from the same values. The voice gets a direct-path override (`--voice-model` > `CODECAST_VOICE_MODEL` > today's default). Diagnostics are Python `logging` to `workspace/<run>/run.log` plus a per-step dump of the exact provider request/response in `workspace/<run>/llm/`.

**Tech Stack:** Python 3.12, pydantic v2, `python-dotenv` (new), stdlib `logging` and `string.Template`, piper-tts, pytest.

**Spec:** No separate spec. Requirements come from the user's request on 2026-10-20 (conversation) and the failure analysis of `workspace/20261020T114621Z-9ce380`:
1. Duration/word limits come from `.env`; when absent, today's values apply.
2. The director prompt is generic; its numbers come from the same values.
3. `codecast run --voice-model <path.onnx>` uses the user's own voice (`voices/rupak-best-mos-epoch=44.onnx`); without it, the default piper voice route is unchanged.
4. Better error messages.
5. Logs, plus saved LLM prompts, so failures can be reviewed.
6. (From the analysis) an empty model reply must not crash the run with `content: null`.
7. (User review of Task 1) two empty model replies in a row stop the run with `CodecastError("model returned empty replies")` instead of nudging again.
8. `codecast run --run <id> --compute-video-length` reports the video length of an existing run's script for zero LLM tokens; without the option, `run` generates the video as today.

## Global Constraints

- Defaults: duration 50.0–300.0 s (the max rises from today's 80 s to 5 minutes, per the user on 2026-10-20), words 110–200, target 155. Apart from the higher duration ceiling, a run with no `.env` behaves the same as `main`. Existing tests and README text that pin 80 s are updated to 300 s.
- Precedence everywhere: real environment variable > `.env` file > YAML config / packaged default. `.env` never overwrites a variable already set in the shell.
- Env variable names (exact): `CODECAST_DURATION_MIN_S`, `CODECAST_DURATION_MAX_S`, `CODECAST_MIN_WORDS`, `CODECAST_MAX_WORDS`, `CODECAST_TARGET_WORDS`, `CODECAST_VOICE_MODEL`.
- An invalid limit (non-numeric, min > max, target outside [min, max], negative) fails at startup with `ConfigError` (exit 7) naming the variable and its value.
- Voice precedence: `--voice-model` > `CODECAST_VOICE_MODEL` > `voice.piper_voice` in `voice.voices_dir` (today's route). An explicitly given voice that is missing is an error (exit 7), never a silent fall back to another voice.
- Never write API keys to `run.log` or `llm/` dumps.
- `pytest -q` stays offline and green; no new test needs network, an API key, or the 63 MB voice file.
- `voices/*.onnx` is not committed (gitignored; GitHub rejects files over 100 MB and warns over 50 MB).

## Review Focus

1. **Switching voices on a resumed run** (`--from-stage voice --voice-model other.onnx`) must re-synthesize, not reuse cached WAVs from the old voice. Today the cache key is `engine.name + text`, and both voices are `piper`. → test in Task 5.
2. **A `.env` that raises the max duration but leaves word limits alone** (the user's plan: max 600 s) must simply stop duration failures, not start new ones; and `codecast eval` on that output must judge it by the limits the run used, not the defaults. → tests in Task 3.
3. **`--voice-model` pointing at an `.onnx` without its `.onnx.json`** (piper needs both) must say so in one line, not raise a piper stack trace. → test in Task 5.
4. **Windows paths and quoting in `.env`** (`CODECAST_VOICE_MODEL="C:\2026\rkumar\CodeCast\voices\rupak-best-mos-epoch=44.onnx"`, with an `=` inside the file name) must load as given. → test in Task 2.
5. **A provider error before any request was captured** (for example, bad auth on step 1) must still produce a readable message with no broken "see request file" pointer. → test in Task 7.

---

## File Structure

| File | Change | Responsibility |
|---|---|---|
| `src/codecast/settings.py` | create | load `.env`; read and validate `CODECAST_*` overrides |
| `src/codecast/config.py` | modify | new `LimitsConfig`; `VoiceConfig.model_path`; apply env overrides in `load_config` |
| `src/codecast/defaults.yaml` | modify | `limits:` section with today's values |
| `src/codecast/grounding/validator.py` | modify | word limits from `ValidationContext.limits` |
| `src/codecast/voice/timeline.py` | modify | `duration_feedback` takes limits; voice-aware cache key |
| `src/codecast/voice/engines.py` | modify | direct model path; `voice_id`; clear missing-file errors |
| `src/codecast/pipeline.py`, `eval.py`, `report.py` | modify | use `cfg.limits`; record limits + voice in the report; eval uses recorded limits |
| `src/codecast/agent/prompts/director.md` | modify | `$placeholders` for every limit-derived number |
| `src/codecast/agent/loop.py` | modify | render prompt; skip empty assistant turns; capture + log each step |
| `src/codecast/agent/capture.py` | create | write `llm/step-NNN.request.json` / `.response.json` / `system_prompt.md` |
| `src/codecast/runlog.py` | create | attach/detach the per-run `run.log` handler |
| `src/codecast/llm/errors.py`, `openai.py`, `anthropic.py` | modify | structured error fields; expose `last_request` |
| `src/codecast/cli.py` | modify | load `.env`; `--voice-model`, `--verbose`, `--compute-video-length`; better failure print |
| `src/codecast/estimate.py` | create | measure an existing run's storyboard with the chosen voice, no LLM |
| `.env.example`, `.gitignore`, `README.md`, `pyproject.toml` | modify | document and wire it up |

---

### Task 1: An empty model reply no longer crashes the run

**Files:**
- Modify: `src/codecast/agent/loop.py:141` (the `self.messages.append(resp.message)` line)
- Modify: `src/codecast/llm/openai.py:75`
- Test: `tests/unit/test_agent_loop.py`, `tests/unit/test_llm_openai.py`

**Interfaces:**
- Produces: `MAX_EMPTY_REPLIES = 2` in `loop.py`. Behavior: an assistant message with no parts is never added to history; the nudge still is. Two empty replies in a row inside one `_loop` call raise `CodecastError("model returned empty replies", stage="agent")` (exit 1); a reply with parts resets the count. Text-only replies keep today's `MAX_NUDGES` rule.
- Note: today's loop is already bounded (a third tool-less reply raises `BudgetExceeded`), so this guard gives a clearer, earlier stop rather than fixing an infinite loop.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_agent_loop.py
def test_empty_reply_is_not_added_to_history(repo):
    empty = ChatResponse(Message("assistant", []), "end_turn", Usage())
    agent, provider, _ = make(repo, [empty, tool_call("submit_storyboard", {"storyboard": valid_submission()})])
    agent.run(msgs(repo))
    second = provider.requests[1].messages
    assert all(m.parts for m in second if m.role == "assistant")
    assert second[-1].role == "user" and "did not call a tool" in second[-1].text()

def test_two_empty_replies_stop_the_run(repo):
    empty = ChatResponse(Message("assistant", []), "end_turn", Usage())
    agent, provider, _ = make(repo, [empty, empty, empty, empty, empty])
    with pytest.raises(CodecastError, match="model returned empty replies"):
        agent.run(msgs(repo))
    assert len(provider.requests) == 2

# tests/unit/test_llm_openai.py
def test_assistant_without_text_or_calls_sends_empty_string():
    req = ChatRequest(messages=[Message("user", [TextPart("hi")]), Message("assistant", [])], tools=[])
    body = build_request(req, "m", OPENAI_CAPS, base_url=None)
    assert body["messages"][1]["content"] == ""
```
(Import `ChatResponse`, `Message` from `codecast.llm.types` in the loop test; reuse the existing `repo` fixture.)

- [ ] **Step 2: Run them to verify they fail**

Run: `pytest tests/unit/test_agent_loop.py::test_empty_reply_is_not_added_to_history tests/unit/test_agent_loop.py::test_two_empty_replies_stop_the_run tests/unit/test_llm_openai.py::test_assistant_without_text_or_calls_sends_empty_string -v`
Expected: all three FAIL (empty assistant message present; three requests sent before `BudgetExceeded`; `content` is `None`).

- [ ] **Step 3: Implement**

In `loop.py`, append `resp.message` only when `resp.message.parts` is non-empty (still log it to the transcript), and count consecutive empty replies against `MAX_EMPTY_REPLIES` before the nudge is appended. In `openai.py:75`, use `m.text() or (None if calls else "")`, which needs `calls` computed before the dict.

- [ ] **Step 4: Run the full unit suite**

Run: `pytest tests/unit -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/codecast/agent/loop.py src/codecast/llm/openai.py tests/unit/test_agent_loop.py tests/unit/test_llm_openai.py
git commit -m "fix: drop empty assistant turns and stop after two in a row"
```

---

### Task 2: `.env` loading and the `limits` config section

**Files:**
- Create: `src/codecast/settings.py`, `.env.example`
- Modify: `src/codecast/config.py`, `src/codecast/defaults.yaml`, `src/codecast/cli.py` (`main`), `pyproject.toml` (add `"python-dotenv>=1.0"`)
- Test: `tests/unit/test_settings.py` (new), `tests/unit/test_config.py`

**Interfaces:**
- Produces:
  - `settings.load_env_file(path: Path = Path(".env")) -> bool` — loads with `override=False`; returns whether a file was found.
  - `settings.limit_overrides(env: Mapping[str, str] = os.environ) -> dict[str, float | int]` — keys `duration_min_s`, `duration_max_s`, `min_words`, `max_words`, `target_words`, only for variables that are set; raises `ConfigError` on a non-numeric value.
  - `settings.voice_model_from_env(env=os.environ) -> str | None`
  - `config.LimitsConfig(BaseModel)`: `duration_min_s: float = 50.0`, `duration_max_s: float = 300.0`, `min_words: int = 110`, `max_words: int = 200`, `target_words: int = 155`; property `duration_range -> tuple[float, float]`; a model validator enforcing 0 ≤ min ≤ max and min_words ≤ target_words ≤ max_words.
  - `Config.limits: LimitsConfig`; `VoiceConfig.model_path: str | None = None`.
  - `load_config(path=None, *, env: Mapping[str, str] | None = None)`: after the YAML merge, applies `limit_overrides(env)` over `data["limits"]` and `voice_model_from_env(env)` into `data["voice"]["model_path"]`. `env=None` means `os.environ`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_settings.py
def test_no_env_keeps_todays_limits():
    cfg = load_config(env={})
    assert cfg.limits.duration_range == (50.0, 300.0)
    assert (cfg.limits.min_words, cfg.limits.max_words, cfg.limits.target_words) == (110, 200, 155)

def test_env_overrides_duration_only():
    cfg = load_config(env={"CODECAST_DURATION_MIN_S": "0", "CODECAST_DURATION_MAX_S": "600"})
    assert cfg.limits.duration_range == (0.0, 600.0)
    assert cfg.limits.max_words == 200

@pytest.mark.parametrize("env, needle", [
    ({"CODECAST_DURATION_MAX_S": "ten"}, "CODECAST_DURATION_MAX_S"),
    ({"CODECAST_DURATION_MIN_S": "400"}, "duration"),         # 400 > default max 300
    ({"CODECAST_TARGET_WORDS": "500"}, "target_words"),
])
def test_bad_limits_fail_with_the_name(env, needle):
    with pytest.raises(ConfigError, match=needle):
        load_config(env=env)

def test_env_file_does_not_override_shell(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("CODECAST_MAX_WORDS=300\n", encoding="utf-8")
    monkeypatch.setenv("CODECAST_MAX_WORDS", "250")
    assert load_env_file(tmp_path / ".env") is True
    assert os.environ["CODECAST_MAX_WORDS"] == "250"

def test_env_file_windows_path_with_equals(tmp_path, monkeypatch):   # Review Focus 4
    monkeypatch.delenv("CODECAST_VOICE_MODEL", raising=False)
    p = r"C:\2026\rkumar\CodeCast\voices\rupak-best-mos-epoch=44.onnx"
    (tmp_path / ".env").write_text(f'CODECAST_VOICE_MODEL="{p}"\n', encoding="utf-8")
    load_env_file(tmp_path / ".env")
    assert os.environ["CODECAST_VOICE_MODEL"] == p
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/test_settings.py -v`
Expected: FAIL (`ModuleNotFoundError: codecast.settings`).

- [ ] **Step 3: Implement** `settings.py` and the config changes as specified in Interfaces; add the `limits:` block to `defaults.yaml` with the default values; call `load_env_file()` as the first line of `cli.main` (current working directory's `.env`). Write `.env.example` listing all six variables commented out, with their defaults and a one-line note on each. Run `pip install -e ".[dev]"` after editing `pyproject.toml`.

- [ ] **Step 4: Run tests**

Run: `pytest tests/unit/test_settings.py tests/unit/test_config.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/codecast/settings.py src/codecast/config.py src/codecast/defaults.yaml src/codecast/cli.py pyproject.toml .env.example tests/unit/test_settings.py
git commit -m "feat: load .env and make duration/word limits configurable"
```

---

### Task 3: Every limit check reads `cfg.limits`

**Files:**
- Modify: `src/codecast/grounding/validator.py:24-28,168-169,186,211-213,328`; `src/codecast/voice/timeline.py:13,132-138`; `src/codecast/pipeline.py:24,43,83,153,184`, and the `ValidationContext(...)` call; `src/codecast/eval.py:19,103`; `src/codecast/report.py` (`RunReport`)
- Test: `tests/unit/test_validator_rules.py`, `tests/unit/test_timeline.py`, `tests/unit/test_eval.py`, `tests/e2e/test_pipeline_offline.py`

**Interfaces:**
- Consumes: `LimitsConfig`, `Config.limits` (Task 2).
- Produces:
  - `ValidationContext.limits: LimitsConfig = field(default_factory=LimitsConfig)`.
  - `structure_issues(sb: Storyboard, limits: LimitsConfig = LimitsConfig()) -> list[Issue]`.
  - `duration_feedback(timeline, sb, *, limits: LimitsConfig) -> str` (replaces `target=`).
  - `RunReport.limits: LimitsConfig | None = None`, filled by the pipeline from `cfg.limits`.
  - `RunOptions.duration_range` is removed; the pipeline uses `cfg.limits.duration_range`.
  - The module constants `DURATION_RANGE_S`, `MIN_WORDS`, `MAX_WORDS`, `TARGET_WORDS` are deleted; `LimitsConfig` defaults replace them.

- [ ] **Step 1: Write the failing tests**

```python
# test_validator_rules.py
def test_word_limits_come_from_context(repo):
    sb = <existing ~155-word valid storyboard helper>
    issues = structure_issues(sb, LimitsConfig(min_words=10, max_words=50, target_words=30))
    assert [i.code for i in issues] == ["WORD_COUNT"]
    assert "need 10-50" in issues[0].message and "~30" in issues[0].message

# test_timeline.py
def test_feedback_uses_given_range():
    out = json.loads(duration_feedback(<timeline of 85 s>, <sb>, limits=LimitsConfig(duration_max_s=120)))
    assert out["target"] == [50.0, 120.0]

# test_eval.py   (Review Focus 2)
def test_eval_uses_limits_recorded_in_report(tmp_path):
    # output dir with a 400 s video whose run_report.json records limits.duration_max_s = 600
    assert <evaluate(...) duration check> passes
    # same output with no "limits" in the report (older run) falls back to the 50-300 default and fails

# test_pipeline_offline.py   (Review Focus 2)
def test_long_video_passes_when_max_duration_raised(...):
    # existing offline pipeline with SilenceEngine(seconds_per_word=0.6) → about 93 s
    cfg = load_config(env={"CODECAST_DURATION_MAX_S": "600"})
    report = run(opts, cfg, provider=..., engine=...)
    assert report.exit_code == 0 and report.validation.duration_rounds == 0
    assert report.limits.duration_max_s == 600
```

- [ ] **Step 2: Run to verify failure** — `pytest tests/unit/test_validator_rules.py tests/unit/test_timeline.py tests/unit/test_eval.py tests/e2e/test_pipeline_offline.py -v`; the new tests FAIL.

- [ ] **Step 3: Implement** the Interfaces. `eval.py` reads `report["limits"]` when present and otherwise uses `LimitsConfig()`. Grep for any remaining import of the deleted constants (`grep -rn "DURATION_RANGE_S\|MIN_WORDS\|MAX_WORDS\|TARGET_WORDS" src tests`) and update each one.

- [ ] **Step 4: Run** `pytest -q` → PASS.

- [ ] **Step 5: Commit** — `git commit -m "feat: drive validator, duration loop, compose and eval from cfg.limits"`

---

### Task 4: Generic director prompt

**Files:**
- Modify: `src/codecast/agent/prompts/director.md` (lines 3, 45, 85), `src/codecast/agent/loop.py:28-29`, `src/codecast/pipeline.py` (the `load_director_prompt()` call)
- Test: `tests/unit/test_agent_loop.py`

**Interfaces:**
- Consumes: `LimitsConfig` (Task 2).
- Produces: `load_director_prompt(limits: LimitsConfig = LimitsConfig()) -> str`, rendered with `string.Template.substitute` (the prompt contains JSON braces and no `$`, so `str.format` and Jinja are unsafe and `Template` is not). Placeholders: `$duration_min`, `$duration_max` (formatted `:g`, so 50.0 → `50`), `$min_words`, `$max_words`, `$aim_lo`, `$aim_hi` (= target ∓ 15, clamped to [min_words, max_words]).
- Rewrites (my call, keeps today's wording at default values):
  - line 3: `…director of a $duration_min–$duration_max second developer explainer video…`
  - line 45: `Total spoken words: **$min_words–$max_words** (hard limits); aim for **$aim_lo–$aim_hi**.` The fixed "6–8 scenes with 13–14 sentences" shape becomes "keep sentences to 10–12 words and split the total across the scenes", because a fixed sentence count only fits ~155 words.
  - line 85: `If the total is outside $duration_min–$duration_max s…`

- [ ] **Step 1: Write the failing tests**

```python
def test_prompt_defaults_render_todays_numbers():
    p = load_director_prompt()
    assert "50–300 second" in p and "**110–200**" in p and "**140–170**" in p and "$" not in p

def test_prompt_follows_limits():
    p = load_director_prompt(LimitsConfig(duration_max_s=600, max_words=1200, target_words=1000))
    assert "50–600 second" in p and "**110–1200**" in p and "**985–1015**" in p
```

- [ ] **Step 2: Run** `pytest tests/unit/test_agent_loop.py -k prompt -v` → FAIL.
- [ ] **Step 3: Implement**; the pipeline passes `cfg.limits`.
- [ ] **Step 4: Run** `pytest -q` → PASS.
- [ ] **Step 5: Commit** — `git commit -m "feat: render director prompt limits from config"`

---

### Task 5: `--voice-model` for a custom piper voice

**Files:**
- Modify: `src/codecast/voice/engines.py` (`PiperEngine`, `piper_model_path`, `engine_status`, `select_engine`), `src/codecast/voice/timeline.py:117` (cache key), `src/codecast/cli.py` (`run` and `doctor` parsers), `src/codecast/report.py` (`RunReport.tts_voice: str | None = None`), `src/codecast/pipeline.py` (set `rep.tts_voice`), `.gitignore`
- Test: `tests/unit/test_engines.py`, `tests/unit/test_timeline.py`, `tests/unit/test_cli.py`

**Interfaces:**
- Consumes: `VoiceConfig.model_path` (Task 2; already filled from `CODECAST_VOICE_MODEL`).
- Produces:
  - CLI: `codecast run … --voice-model PATH` and `codecast doctor --voice-model PATH`; when given, `cli.main` sets `cfg.voice.model_path = str(PATH)` after `load_config`, which gives the CLI > env > default order.
  - `piper_model_path(cfg) -> Path`: `Path(cfg.model_path).expanduser()` if set, else today's `voices_path / f"{piper_voice}.onnx"`.
  - `engine_status`: when `model_path` is set, `"piper"` is `"ok"` or `"custom voice not found: <path>"` or `"custom voice config missing: <path>.json"`.
  - `select_engine`: if `model_path` is set and piper is not `"ok"`, raise `ConfigError(<that status>)` without falling back to `say`/`edge`.
  - Every engine gets `voice_id: str` (piper: the model file stem; say/edge: their voice name). `synthesize_storyboard` keys the cache on `f"{engine.name}\x00{engine.voice_id}\x00{text}"`.
  - `rep.tts_voice` = `engine.voice_id`.

- [ ] **Step 1: Write the failing tests**

```python
# test_engines.py
def test_custom_model_path_wins(tmp_path):
    m = tmp_path / "me.onnx"; m.write_bytes(b"x"); (tmp_path / "me.onnx.json").write_text("{}")
    assert piper_model_path(VoiceConfig(model_path=str(m))) == m

def test_missing_custom_voice_is_an_error_not_a_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(engines, "_piper_installed", lambda: True)
    monkeypatch.setattr(engines, "_say_available", lambda v: True)
    with pytest.raises(ConfigError, match="custom voice not found"):
        select_engine(VoiceConfig(model_path=str(tmp_path / "nope.onnx")), allow_network=False)

def test_custom_voice_without_json_says_so(tmp_path, monkeypatch):   # Review Focus 3
    monkeypatch.setattr(engines, "_piper_installed", lambda: True)
    m = tmp_path / "me.onnx"; m.write_bytes(b"x")
    with pytest.raises(ConfigError, match=r"me\.onnx\.json"):
        select_engine(VoiceConfig(model_path=str(m)), allow_network=False)

# test_timeline.py   (Review Focus 1)
def test_cache_is_per_voice(tmp_path):
    a, b = SilenceEngine(), SilenceEngine(); a.voice_id, b.voice_id = "default", "rupak"
    synthesize_storyboard(sb, a, tmp_path, gap_s=0.15, tail_s=0.5)
    calls = []; b.synthesize = lambda t, p: (calls.append(t), SilenceEngine().synthesize(t, p))
    synthesize_storyboard(sb, b, tmp_path, gap_s=0.15, tail_s=0.5)
    assert calls   # nothing reused from voice "default"

# test_cli.py
def test_voice_model_flag_beats_env(monkeypatch, tmp_path):
    monkeypatch.setenv("CODECAST_VOICE_MODEL", "from-env.onnx")
    # patch pipeline_run to capture cfg; run main(["run", "--repo", ".", "--voice-model", "flag.onnx"])
    assert captured_cfg.voice.model_path == "flag.onnx"
```
(Add `voice_id = "silence"` to `tests/fakes.py::SilenceEngine`.)

- [ ] **Step 2: Run** `pytest tests/unit/test_engines.py tests/unit/test_timeline.py tests/unit/test_cli.py -v` → the new tests FAIL.
- [ ] **Step 3: Implement** the Interfaces. Add `voices/*.onnx` to `.gitignore` (the `.onnx.json` stays committable).
- [ ] **Step 4: Run** `pytest -q` → PASS. Manual check (needs the real file, not part of the suite): `codecast doctor --voice-model "voices/rupak-best-mos-epoch=44.onnx"` shows `[OK  ] tts: piper`.
- [ ] **Step 5: Commit** — `git commit -m "feat: --voice-model / CODECAST_VOICE_MODEL for a custom piper voice"`

---

### Task 6: `run.log` and saved LLM requests

**Files:**
- Create: `src/codecast/runlog.py`, `src/codecast/agent/capture.py`
- Modify: `src/codecast/pipeline.py` (`run`), `src/codecast/agent/loop.py` (`__init__`, `_loop`, `_complete`), `src/codecast/llm/openai.py` and `src/codecast/llm/anthropic.py` (`complete`), `src/codecast/llm/fake.py`, `src/codecast/report.py` (`ReportBuilder.fail`), `src/codecast/cli.py` (`--verbose`)
- Test: `tests/unit/test_capture.py` (new), `tests/e2e/test_pipeline_offline.py`

**Interfaces:**
- Produces:
  - `runlog.attach(ws: Path, *, verbose: bool) -> Callable[[], None]`: adds a DEBUG `FileHandler` on logger `"codecast"` writing to `ws / "run.log"` (append, UTF-8, format `%(asctime)s %(levelname)s %(name)s: %(message)s`), plus a stderr handler at INFO when `verbose`, else WARNING; returns a detach function. The pipeline attaches right after `ws.mkdir` and detaches in a `finally`.
  - Each provider sets `self.last_request: dict | None` to the exact body it is about to send (OpenAI: `build_request(...)`; Anthropic: its kwargs dict) before the SDK call. `FakeProvider` sets a plain summary dict so the offline tests cover the path.
  - `capture.LLMCapture(dir: Path)` with `system_prompt(text: str) -> None` (writes `system_prompt.md` once), `request(step: int, body: dict | None) -> Path | None`, `response(step: int, data: dict) -> Path` writing `step-{step:03d}.request.json` / `step-{step:03d}.response.json`, and `last_request_path: Path | None`. Errors get `{"error": {...}}` as the response file.
  - `DirectorAgent(..., capture: LLMCapture | None = None)`; the pipeline passes `LLMCapture(ws / "llm")`.
  - What the agent logs per step (logger `codecast.agent`): INFO `step N: <tool names> | stop=<stop> | in=<tokens> out=<tokens>`; INFO for each `submit_storyboard` result (`accepted` or the error codes); WARNING for an empty reply / nudge. Pipeline (logger `codecast.pipeline`): INFO stage start/end with seconds, engine + voice, limits in effect, duration feedback; ERROR with full traceback on failure.
  - `ReportBuilder.fail` stores a traceback for every exception, including `CodecastError` (today it is `null`), formatted from the chained cause.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_capture.py
def test_request_and_response_written(tmp_path):
    c = LLMCapture(tmp_path / "llm")
    c.system_prompt("SYS"); p = c.request(3, {"messages": []}); c.response(3, {"ok": True})
    assert p.name == "step-003.request.json" and (tmp_path / "llm/step-003.response.json").is_file()
    assert (tmp_path / "llm/system_prompt.md").read_text(encoding="utf-8") == "SYS"
    assert c.last_request_path == p

def test_no_api_key_in_capture(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret-123")
    # run a FakeProvider agent with capture, then:
    assert "sk-secret-123" not in "".join(f.read_text(encoding="utf-8") for f in (tmp_path / "llm").iterdir())

# tests/e2e/test_pipeline_offline.py
def test_failed_run_leaves_log_and_requests(...):
    # FakeProvider script ending in ProviderError("boom")
    report = run(...)
    ws = workspace / report.run_id
    log = (ws / "run.log").read_text(encoding="utf-8")
    assert "stage agent" in log and "Traceback" in log
    assert sorted(p.name for p in (ws / "llm").glob("step-*.request.json"))
    assert report.failure.traceback is not None
```

- [ ] **Step 2: Run** `pytest tests/unit/test_capture.py tests/e2e/test_pipeline_offline.py -v` → FAIL.
- [ ] **Step 3: Implement** the Interfaces. The capture runs inside `_complete`: write the request before the call and the response (or error) after it, so a request that fails has its body on disk.
- [ ] **Step 4: Run** `pytest -q` → PASS.
- [ ] **Step 5: Commit** — `git commit -m "feat: per-run run.log and saved LLM requests/responses"`

---

### Task 7: Readable failure messages

**Files:**
- Modify: `src/codecast/llm/errors.py`, `src/codecast/llm/openai.py:122-133` (`map_error`), `src/codecast/llm/anthropic.py` (its `map_error`), `src/codecast/agent/loop.py:109-123` (`_complete`), `src/codecast/cli.py:58-66`
- Test: `tests/unit/test_llm_openai.py`, `tests/unit/test_llm_anthropic.py`, `tests/unit/test_agent_loop.py`, `tests/unit/test_cli.py`

**Interfaces:**
- Consumes: `LLMCapture.last_request_path` (Task 6).
- Produces:
  - `LLMError.__init__(self, message: str, *, provider: str | None = None, status: int | None = None, error_type: str | None = None, param: str | None = None)`, with the fields kept as attributes. `str(e)` stays the plain message.
  - `map_error` fills them from the SDK exception (`e.status_code`; `e.body["error"]["type" | "param" | "message"]` for OpenAI, `e.body["error"]["type" | "message"]` for Anthropic), with `message` = the provider's own message text, not the whole repr.
  - `DirectorAgent._complete` builds the message with this exact copy:
    `agent step {step}: {Provider} rejected the request (HTTP {status} {error_type}{", " + param if param}): {message}`
    then, only if a request was captured, `\n  request sent: {path}`, and always `\n  full log: {ws}/run.log`. Provider unavailable / auth / context errors keep their current wording with the same step prefix and log line.
  - CLI failure output, on stderr:
    ```
    codecast FAILED (exit 1) at stage agent
      agent step 20: OpenAI rejected the request (HTTP 400 invalid_request_error, messages.[49].content): Invalid value for 'content': expected a string, got null.
      request sent: workspace/<id>/llm/step-020.request.json
      full log: workspace/<id>/run.log
      run report: workspace/<id>/run_report.json
    ```

- [ ] **Step 1: Write the failing tests**

```python
# test_llm_openai.py
def test_map_error_extracts_fields():
    e = <openai.BadRequestError built with status 400 and body {"error": {"message": "Invalid value for 'content'...", "type": "invalid_request_error", "param": "messages.[49].content"}}>
    m = map_error(e)
    assert (m.status, m.error_type, m.param) == (400, "invalid_request_error", "messages.[49].content")
    assert str(m).startswith("Invalid value for 'content'")

# test_agent_loop.py
def test_provider_error_message_names_step_and_request(repo, tmp_path):
    err = ProviderError("bad content", provider="openai", status=400, error_type="invalid_request_error", param="messages.[3].content")
    agent, _, _ = make(repo, [err], capture=LLMCapture(tmp_path / "llm"))
    with pytest.raises(CodecastError) as ei: agent.run(msgs(repo))
    assert ei.value.message.startswith("agent step 1: OpenAI rejected the request (HTTP 400 invalid_request_error, messages.[3].content): bad content")
    assert "step-001.request.json" in ei.value.message

def test_error_before_capture_has_no_request_line(repo):   # Review Focus 5
    agent, _, _ = make(repo, [AuthError("bad key", provider="openai", status=401)])
    with pytest.raises(ConfigError) as ei: agent.run(msgs(repo))
    assert "request sent" not in ei.value.message

# test_cli.py
def test_failure_output_lists_paths(capsys, ...):
    # patch pipeline_run to return a failed report
    assert "full log:" in capsys.readouterr().err and "run report:" in ...
```

- [ ] **Step 2: Run** the four test files → the new tests FAIL.
- [ ] **Step 3: Implement** the Interfaces. The workspace path reaches the agent through a new `DirectorAgent(..., log_path: Path | None = None)` argument.
- [ ] **Step 4: Run** `pytest -q` → PASS.
- [ ] **Step 5: Update `README.md`** (§4 Engineering notes and §5 Run): the `.env` variables, `--voice-model`, `--verbose`, `run.log`, `llm/`. Then commit — `git commit -m "feat: readable LLM failure messages with pointers to log and request"`

---

### Task 8: `--compute-video-length` measures an existing run for zero tokens

**Files:**
- Create: `src/codecast/estimate.py`
- Modify: `src/codecast/cli.py` (`run` parser and `main`)
- Test: `tests/unit/test_estimate.py` (new), `tests/unit/test_cli.py`

**Interfaces:**
- Consumes: `Config.limits` (Task 2), `select_engine` honouring `VoiceConfig.model_path` and `engine.voice_id` (Task 5), `synthesize_storyboard` (existing, `voice/timeline.py`).
- Produces:
  - CLI: `codecast run --run <id> --compute-video-length [--voice-model PATH] [--workspace DIR] [--config FILE]`. `--repo` becomes optional at parse time; `main` raises `ConfigError` (exit 7) when `--repo` is missing without `--compute-video-length`, when `--compute-video-length` is given without `--run`, or when it is combined with `--from-stage`.
  - `estimate.LengthEstimate(BaseModel)`: `run_id: str`, `engine: str`, `voice: str`, `per_scene_seconds: list[float]`, `total_s: float`, `spoken_words: int`, `limits: tuple[float, float]`, `within_limits: bool`.
  - `estimate.estimate_length(ws: Path, cfg: Config, *, engine: TTSEngine | None = None) -> LengthEstimate`: reads `ws / "storyboard.json"`, synthesizes with the selected voice into `ws / "audio" / "estimate"` (the per-voice cache from Task 5 makes a repeat call instant), writes `ws / "length_estimate.json"`, and returns the estimate. It never creates an LLM provider and never runs the API-key preflight check. Its total is `Timeline.total`, the same number the final ffprobe check is compared against, so it is the real length rather than a words-per-second guess.
  - Missing storyboard: `ConfigError("no storyboard.json in <ws>; --compute-video-length measures the script of an existing run")`.
  - CLI output on stdout, exit 0 whether or not the length is inside the limits (my call: it is a measurement, not a check):
    ```
    video length for run <id> (piper voice rupak-best-mos-epoch=44):
      scene 1  title     8.1 s
      scene 2  bullets   8.7 s
      ...
      total 85.0 s, 189 spoken words: within the 50–300 s limit
    saved: workspace/<id>/length_estimate.json
    ```

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_estimate.py
def test_estimate_uses_storyboard_and_never_the_llm(tmp_path, monkeypatch):
    ws = tmp_path / "run1"; ws.mkdir()
    (ws / "storyboard.json").write_text(<accepted storyboard from tests.unit.storyboards>.model_dump_json(), encoding="utf-8")
    monkeypatch.setattr("codecast.llm.registry.create_provider", lambda *a, **k: pytest.fail("LLM called"))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False); monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    est = estimate_length(ws, load_config(env={}), engine=SilenceEngine(seconds_per_word=0.4))
    assert est.total_s > 0 and len(est.per_scene_seconds) == <number of scenes>
    assert est.limits == (50.0, 300.0) and est.within_limits
    assert (ws / "length_estimate.json").is_file()

def test_estimate_without_storyboard_explains(tmp_path):
    with pytest.raises(ConfigError, match="measures the script of an existing run"):
        estimate_length(tmp_path, load_config(env={}), engine=SilenceEngine())

# tests/unit/test_cli.py
def test_compute_video_length_needs_no_repo(tmp_path, capsys, monkeypatch):
    # workspace/run1/storyboard.json present; patch select_engine to return SilenceEngine()
    assert main(["run", "--run", "run1", "--compute-video-length", "--workspace", str(tmp_path)]) == 0
    assert "total" in capsys.readouterr().out

def test_compute_video_length_requires_run():
    assert main(["run", "--compute-video-length"]) == 7

def test_run_without_repo_still_rejected():
    assert main(["run"]) == 7
```

- [ ] **Step 2: Run** `pytest tests/unit/test_estimate.py tests/unit/test_cli.py -v` → the new tests FAIL.
- [ ] **Step 3: Implement** the Interfaces.
- [ ] **Step 4: Run** `pytest -q` → PASS. Manual check (real voice and ffmpeg, not in the suite): `codecast run --run 20261020T114621Z-9ce380 --compute-video-length --voice-model "voices/rupak-best-mos-epoch=44.onnx"` prints a per-scene table and a total.
- [ ] **Step 5: Add a line to `README.md` §5, then commit** — `git commit -m "feat: --compute-video-length measures an existing run's script without the LLM"`

---

## Not in this plan (deliberately)

- With the 300 s default but the 200-word cap unchanged, videos stay around 1 minute; the higher ceiling only stops duration failures. Making videos *longer* is a matter of raising `CODECAST_MAX_WORDS` / `CODECAST_TARGET_WORDS` together with the duration; the plan makes that possible but does not change the defaults.
- Better duration revisions by the model (the cause of the `NARRATION_UNCHANGED` loop) are out of scope; with the max duration raised they stop mattering for this user.
- Other `voice` YAML keys (`gap_s`, `tail_s`, …) stay YAML-only.
