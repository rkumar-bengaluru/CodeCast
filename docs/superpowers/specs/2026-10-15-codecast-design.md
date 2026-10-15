# CodeCast — Design Spec

- **Date:** 2026-10-15
- **Status:** Draft for review
- **Supersedes:** the architecture sketch in `README.md` where they conflict (see §12)

## 1. Purpose and success criteria

CodeCast turns a GitHub repository into a 50–60 second developer explainer video (MP4 + SRT + storyboard), grounded in the repo's actual files.

**Phase A (this spec)** is a judged submission:

- `run.yaml` passes on a fresh clone on macOS.
- A committed demo video for `https://github.com/rkumar-bengaluru/video-classifier` looks good and contains zero invented numbers.
- The code is repo-agnostic (no demo-specific paths, names or JSON keys); quality is only *guaranteed* on the demo repo, plus one smoke test on a repo without metrics.

**Phase B** (out of scope, §11) makes CodeCast a tool used on arbitrary repos.

### Targets

| Metric | Target |
|---|---|
| Ungrounded numbers / identifiers in narration or on-screen text | 0 (enforced by code) |
| Duration (ffprobe) | 50–60 s |
| Caption coverage of narration time | ≥ 95% |
| Cost per video | < $0.10 — measured and reported as-is, even if missed |
| End-to-end time | < 5 min |
| Human clarity rating (1–5) | > 4 |

### Decisions taken during brainstorming

- **Fully agentic director:** an LLM agent with read-only tools explores the repo and produces the storyboard. Code validates; the LLM never judges groundedness.
- **Provider-agnostic LLM layer** (adapter pattern): Anthropic and OpenAI (+ OpenAI-compatible endpoints) in Phase A.
- **Output:** 16:9, 1920×1080, 30 fps.
- **TTS:** local-first (`piper` → `say` → `edge-tts` opt-in).
- **Playwright** renders every visual scene from HTML templates (it does not record terminals).

## 2. Architecture

```
repo URL / local path
  │
  ▼
[1] Ingest ─────────── shallow clone → workspace/<run_id>/repo (read-only to agent) → RepoMap
  ▼
[2] Director Agent ─── LLM + read-only tools; loops until submit_storyboard accepts
  │     tools: list_dir · read_file · grep · read_json · view_image · submit_storyboard
  │     validation errors are returned as tool results → agent self-corrects
  ▼
[3] Voice ──────────── TTS per sentence → WAVs + measured durations → Timeline
  │     total ∉ [50 s, 60 s] → durations sent back to agent for revision (max 2 rounds)
  ▼
[4] Render ─────────── Playwright: scene templates → PNG keyframes; caption cues → PNG bands
  ▼
[5] Compose ────────── ffmpeg: per-scene segments (motion + caption overlays) → concat → mux audio
  ▼
output/<repo>/  explainer.mp4 · explainer.srt · storyboard.json · run_report.json
```

Principles:

- **Agent decides, code checks.** The agent selects from a fixed scene catalog and fills parameters; it never writes HTML or ffmpeg commands.
- **Agent is harmless.** All tools are read-only, confined to the cloned repo, no shell. Repo content is untrusted (prompt-injection risk).
- **Every stage persists its output** to `workspace/<run_id>/`, and the pipeline can resume from any stage (`--from-stage`).
- **Fail loudly.** Every failure produces a typed exit code and a `run_report.json`; never a silently degraded video.

## 3. Ingest (`codecast/ingest/`)

- `git clone --depth 1 --no-recurse-submodules` with `GIT_LFS_SKIP_SMUDGE=1`; also accepts a local path (development). Public repos only.
- Clone timeout 120 s; abort if checkout > 1 GB (exit 2).
- Citations reference the **HEAD commit SHA**, recorded in `RepoMap` and `Storyboard`. No history or blame is needed.
- **Path sandbox** (`sandbox.py`): every tool path is resolved with `realpath()` and must lie inside the repo root; otherwise rejected. `list_dir` does not follow symlinks.
- **Skip rules:** dirs `node_modules`, `.venv`, `venv`, `vendor`, `dist`, `build`, `.git`, `__pycache__`; `.gitignore` matches; files > 1 MB; LFS pointer files; binaries are listed but unreadable by `read_file`.

```
RepoMap {
  repo_url, commit_sha, root, n_files,
  files:   [{path, size, kind: text|image|binary|lfs_pointer}],
  skipped: [{path, reason: ignored_dir|too_large|symlink_escape|gitignored|lfs_pointer|binary}],
  tree_text            # depth-limited tree with sizes; sent in the agent's first message
}
```

## 4. LLM layer (`codecast/llm/`)

No module outside `llm/` imports a provider SDK. All calls are **synchronous** with a `timeout_s`.

### 4.1 Neutral types (`types.py`)

```python
ContentPart = TextPart(text) | ImagePart(data: bytes, media_type)
            | ToolCallPart(id, name, args: dict) | ToolResultPart(call_id, parts, is_error)
Message(role: "system" | "user" | "assistant" | "tool", parts: list[ContentPart])
ToolSpec(name, description, input_schema: dict)            # JSON Schema
ToolChoice = "auto" | "required" | {"name": str}
ChatRequest(messages, tools, tool_choice, max_tokens, temperature, timeout_s)
ChatResponse(message, stop: "end_turn" | "tool_use" | "max_tokens", usage: Usage)
Usage(input_tokens, output_tokens, cache_read_input_tokens, cache_write_input_tokens)
ProviderCaps(vision, tool_use, prompt_caching, supports_temperature, context_window)
```

### 4.2 Interface and adapters

```python
class LLMProvider(Protocol):
    caps: ProviderCaps
    def complete(self, req: ChatRequest) -> ChatResponse: ...
```

- **`AnthropicProvider`** — Messages API; maps tool_use/tool_result blocks and `tool_choice`; sets `cache_control` on system prompt and tool definitions. Extended thinking is off in Phase A (it is incompatible with forced `tool_choice`); a config enabling both is rejected at startup (exit 7).
- **`OpenAIProvider`** — Chat Completions with configurable `base_url` (covers OpenAI and OpenAI-compatible endpoints such as OpenRouter, Ollama, vLLM). Image quirk: OpenAI `tool` messages cannot carry images, so for one assistant turn the adapter emits **all** `tool` messages first (image results become text `"image <path> returned, see next message"`), then **one** `user` message containing every image from that turn, each annotated `Result of <call_id> view_image path=<path>`. Omits `temperature` when `caps.supports_temperature` is false.
- **`FakeProvider`** — replays scripted responses or a recorded `transcript.jsonl`. Used by all offline tests.
- **Errors** (`errors.py`): `RetryableError` (429, 5xx, timeout; 3 attempts, exponential backoff with jitter), `AuthError`, `ContextLengthError`, `ProviderError`. SDK exceptions never escape the adapter.
- **Registry** (`registry.py`): `provider name → adapter class`. A new provider = one adapter file + one registry entry.
- **Pricing** (`pricing.py`, data in `config/default.yaml`): per-model rates for input, output, cache read and cache write. Rates are copied from the providers' official pricing pages during implementation, not guessed. Unknown model or missing rate → `cost_usd: null` with `cost_unknown_reason`.

### 4.3 Configuration

```yaml
llm:
  provider: anthropic            # anthropic | openai
  model: claude-sonnet-5-5       # required explicitly when provider != anthropic
  api_key_env: ANTHROPIC_API_KEY # or OPENAI_API_KEY
  base_url: null                 # OpenAI-compatible endpoints only
  temperature: 0.3
  max_tokens: 4096
  timeout_s: 120
```

## 5. Director agent (`codecast/agent/`)

### 5.1 Loop (`loop.py`)

```
tools = all tools; if not caps.vision: drop view_image; if not caps.tool_use: exit 7
messages = [system(director prompt + scene catalog), user(task + RepoMap.tree_text)]
for step in 1..max_steps:
    resp = provider.complete(messages, tools, tool_choice="auto")
    if no tool calls:
        nudge "finish by calling submit_storyboard" with tool_choice={"name": "submit_storyboard"}
        (max 2 nudges, then exit 4)
    execute each tool call → ToolResultPart (tool errors returned with is_error=True, never raised)
    if submit_storyboard accepted → return Storyboard
exit 4 (BudgetExceeded), partial run_report written
```

- **Budget:** `max_steps = 30 + min(30, n_files // 50)`, overridable by `--max-steps`. Also a token budget and wall-clock limit (config).
- **Context overflow:** `ContextLengthError` → exit 4, no summarization in Phase A. `run_report.context` records `window`, `input_tokens_at_failure`, `last_turn_tool_outputs: [{tool, call_id, bytes}]`.
- **Transcript** (`transcript.py`): every request/response appended to `workspace/<run_id>/transcript.jsonl`. Images are stored as `images/<sha256>.<ext>` and referenced as `{"type": "image_ref", "path": ...}`. Split OpenAI tool/image messages are logged under the same `call_id`. `FakeProvider` replays transcripts from disk.

### 5.2 Tools (`tools.py`)

| Tool | Args | Returns |
|---|---|---|
| `list_dir` | `path`, `depth=1` | entries with kind and size |
| `read_file` | `path`, `start_line=1` (1-based), `max_lines=400` | `{lines: [numbered], truncated, next_start_line}`; ≤ 32 KB per call |
| `grep` | `pattern`, `path_glob?`, `max_results=50` | `[{file, line, text}]` |
| `read_json` | `path`, `pointer` (RFC 6901) | value at pointer (≤ 32 KB) |
| `view_image` | `path` | image, downscaled to 1568 px long edge |
| `submit_storyboard` | `storyboard` | `accepted` or a list of validation errors |

All path arguments go through the sandbox (§3).

## 6. Data contracts (`codecast/models.py`, pydantic)

### 6.1 Sources (declared once, referenced by id)

```
TextSpan {id, file, start_line, end_line, text}   # text must equal those lines at HEAD exactly
JsonSpan {id, file, pointer, value}               # RFC 6901 pointer; value must equal resolved value
ImageRef {id, file}                               # file exists in RepoMap with kind == image
```

### 6.2 Scene catalog

| type | params | rules |
|---|---|---|
| `title` | `heading`, `subheading?` | heading ≤ 60 chars, subheading ≤ 90 chars, one sentence each |
| `bullets` | `heading`, `items[]` | ≤ 5 items, each ≤ 60 chars; ≤ 12 bullet items across the whole video |
| `code` | `span_id`, `highlight_lines?` | TextSpan, ≤ 18 lines; renderer shows exactly the cited lines with real line numbers |
| `image` | `image_id`, `highlights: [[x,y,w,h]]`, `motion` | rects normalized 0–1 relative to the image, x+w ≤ 1, y+h ≤ 1; `[]` = no highlight; motion `static` or `ken_burns{from_rect, to_rect}` (image-relative) |
| `metrics_table` | `columns[]`, `rows[][]` | each cell is a text label or `{span_id, decimals?}` |
| `diagram` | `mermaid` | syntax checked with `mermaid.parse` if a browser is available at submit time; otherwise fails at render (exit 6) |
| `terminal` | `command_span_ids[]`, `output_span_id?` | commands are TextSpans; output span's file must match the output allowlist (`*.log`, `*.txt`, `*.out`, `logs/**`, `**/output*`; configurable) |
| `outro` | `repo_url`, `cta` | `repo_url` must equal `RepoMap.repo_url` |

Every scene has `narration: [Sentence{text, source_ids[]}]`. Sentences are both TTS units and caption units.

### 6.3 Storyboard

```
Storyboard {version, repo_url, commit_sha, title, sources: {id: span}, scenes: [Scene], llm: {provider, model}}
```

### 6.4 Run report (`run_report.json`)

```
{run_id, started_at, ended_at, repo_url, commit_sha,
 llm: {provider, model, temperature},
 status: ok|failed, exit_code, failure: {stage, code, message, traceback?}?,
 ingest: {n_files, skipped_by_reason},
 budget: {max_steps, steps_used, tool_calls: {name: count}},
 usage: {input, output, cache_read_input, cache_write_input},
 cost_usd: float|null, cost_unknown_reason?,
 context: {window, input_tokens_at_failure, last_turn_tool_outputs}?,
 validation: {storyboard_rounds, duration_rounds, failures_by_code},
 tts_engine, stage_seconds: {ingest, agent, voice, render, compose},
 video: {path, duration_s, size_bytes}?}
```

### 6.5 Exit codes

| Code | Meaning |
|---|---|
| 0 | ok |
| 1 | unhandled error |
| 2 | ingest failed |
| 3 | duration outside [50, 60] s after 2 revision rounds |
| 4 | agent budget, nudges or context exhausted |
| 5 | storyboard validation failed after 4 rounds |
| 6 | render or compose failed (incl. ffprobe post-check) |
| 7 | config, auth or preflight failure |

## 7. Grounding validator (`codecast/grounding/`)

Runs inside `submit_storyboard`. Collects **all** errors, in this order:

1. **Schema** — pydantic.
2. **Source integrity** — TextSpan text equals file lines at HEAD exactly; JsonSpan pointer resolves (JSON parsed with `parse_float=Decimal`) and value matches (numbers by value, strings exactly); ImageRef exists.
3. **Number grounding** — applied to narration **and** all on-screen text (headings, bullets, table labels, mermaid labels). Each number/identifier must be grounded in a source cited by that sentence (narration) or that scene (on-screen text).
4. **Structure** — 4–8 scenes; first is `title`, last is `outro`; total spoken words 110–170 (whitespace tokens of the text as written); each sentence ≤ 30 words; every narrated sentence cites ≥ 1 source.
5. **Scene rules** — §6.2.

Errors are returned as `[{path: "scenes[2].narration[1]", code, message}]`, e.g. `{code: "UNGROUNDED_NUMBER", message: "0.74 not found in cited sources [s3, s7]"}`. Max 4 rounds (exit 5). The duration-revision resubmission (§8) is fully re-validated.

### 7.1 Tokenization (`numbers.py`, single source of truth)

- Normalize `−` (U+2212) to `-`.
- Convert number words (zero–nineteen, tens, hundred, thousand, million and their combinations) to digits before grounding. Ordinals (first, second…) are not numbers. Spoken text is not changed.
- **Identifier:** a token containing both letters and digits (`ViT-B-32`, `large@8`, `MobileCLIP2-S0`, `1k`, `2e3`). Must appear **case-sensitively as a whole token** in a cited span's text. Token boundaries are characters outside `[A-Za-z0-9_@.\-/]`. File paths follow the same rule.
- **Number:** `-?\d+(?:,\d{3})*(?:\.\d+)?%?`.

### 7.2 Number matching rule

Arithmetic uses `Decimal` parsed from the source's literal text (never floats). A narration value `v` is grounded by source value `s` if any holds:

- `v == s`
- `v == round(s, n)` for some `n` < decimals(s)
- `v% == round(s × 100, n)` for some `n` ≤ max(decimals(s) − 2, 0) (percent form, optionally rounded)

…and the rounded result keeps **≥ 2 significant figures**. Both half-up and half-even rounding are accepted. No derived values (sums, counts, differences): the agent must find a span that literally contains the number.

Examples (source 0.7349): `0.7349` ✓, `0.735` ✓, `0.73` ✓, `73.5%` ✓, `0.7` ✗ (1 significant figure), `0.7350` ✗ (more decimals than source). Source 891.2: `891` ✓.

## 8. Voice (`codecast/voice/`)

- **Engines** behind one interface `synthesize(text) -> wav`: `piper` (default; local; voice downloaded once by `codecast fetch-voice`) → `say` (macOS built-in) → `edge-tts` (network; only with `--allow-network-tts`).
- The engine is chosen once by a startup probe; **one engine per video**, never switched mid-run.
- Each sentence → WAV, normalized to 48 kHz mono; measured duration is the source of truth.
- Gaps: 0.15 s between sentences, 0.5 s tail per scene.
- `Timeline {scenes: [{scene_id, start, duration, cues: [{text, start, end}]}], total}`. Sentences longer than two caption lines (~42 chars/line) split into consecutive cues timed in proportion to character count. `explainer.srt` is written from the timeline.
- **Duration loop:** if `total ∉ [50, 60]`, the agent receives `{per_scene_seconds, total, target: [50, 60]}` and resubmits the full storyboard (re-validated, re-voiced). After 2 rounds still out of range → exit 3. No `atempo` stretching.

## 9. Render and compose

### 9.1 Render (`codecast/render/`, Playwright + Jinja2)

- One headless Chromium per run; viewport 1920×1080, `deviceScaleFactor: 1`.
- Templates `templates/<type>.html` + shared `static/base.css`. Fonts (Inter, JetBrains Mono, woff2) and `mermaid.min.js` are vendored; no network access during render.
- Screenshot only after `document.fonts.ready` and the template sets `data-ready`.
- Every template reserves a 160 px bottom caption band.
- Code: Pygments `HtmlFormatter` with real line numbers.
- Image: `object-fit: contain`, highlights drawn in HTML; renderer reports `image_box` so compose can map image-relative rects to frame coordinates.
- **Captions** (the Homebrew ffmpeg build has no libass/`subtitles` filter): each cue is rendered as a transparent **1920×160** PNG in the same fonts.
- Output per scene: `SceneFrames {scene_id, frames[], motion, duration, image_box?}`. Terminal scenes use `sequence` motion: one frame per command typed so far, evenly timed.

### 9.2 Compose (`codecast/compose/`, ffmpeg via `subprocess` argv lists, no shell strings)

1. **Per-scene segment** (30 fps, `yuv420p`, libx264 CRF 20), including that scene's caption overlays (`overlay=0:920:enable='between(t,start,end)'`, times relative to the segment). Filter graphs are written to a file and passed with `-/filter_complex <file>` (ffmpeg ≥ 7 syntax). Segments render in parallel.
   - `static`: loop frame for its duration.
   - `ken_burns`: upscale 2×, crop with x/y/w/h linearly interpolated over `t`, scale back to 1080p (avoids `zoompan` jitter).
   - `sequence`: concat demuxer with per-frame durations.
2. **Concat** segments (concat demuxer, hard cuts; no crossfades in Phase A).
3. **Audio:** concatenate sentence WAVs and gaps per timeline; one-pass `loudnorm=I=-16`; AAC 160 kbps.
4. **Mux** + `-movflags +faststart`.
5. **Post-check** with ffprobe: duration ∈ [50, 60] s and |audio − video| < 0.1 s, else exit 6.

### 9.3 Preflight (`codecast doctor`, also run at startup)

ffmpeg ≥ 7 with `overlay` and `loudnorm` and `-/filter_complex` support; Playwright Chromium installed; a TTS engine available; LLM API key env var set. Failure → exit 7 with a fix-it hint (`brew install ffmpeg`, `playwright install chromium`, `codecast fetch-voice`). Exception: a missing API key is only a **warning** in `codecast doctor` (so setup and offline tests work without a key); `codecast run` treats it as a failure (exit 7).

## 10. CLI, layout, testing, eval

### 10.1 CLI

```
codecast run --repo <url|path> --out output/ [--config path] [--max-steps N]
             [--allow-network-tts] [--from-stage voice|render|compose --run <run_id>]
codecast doctor
codecast fetch-voice
codecast eval output/<repo>/
```

### 10.2 Layout

```
codecast/
  pyproject.toml   run.yaml   config/default.yaml
  src/codecast/
    cli.py  pipeline.py  config.py  models.py
    ingest/     clone.py  repomap.py  sandbox.py
    llm/        types.py  base.py  errors.py  registry.py  pricing.py  anthropic.py  openai.py  fake.py
    agent/      loop.py  tools.py  transcript.py  prompts/director.md
    grounding/  numbers.py  spans.py  validator.py
    voice/      engines.py  timeline.py  fetch_voice.py
    render/     renderer.py  templates/  static/
    compose/    ffmpeg.py  segments.py
    eval.py
  tests/  unit/  integration/  e2e/  fixtures/
  output/video-classifier/   # committed demo: explainer.mp4, .srt, storyboard.json, run_report.json
  docs/eval.md
```

### 10.3 Testing (TDD; default `pytest` is offline and needs no API key)

| Layer | Scope | Mechanism |
|---|---|---|
| Unit | tokenization, matching rules (table-driven incl. §7.2 examples), identifiers, JSON Pointer, span integrity | pure functions |
| Unit | ingest: symlink escape, size caps, skip rules, LFS pointers | temp-dir repos |
| Unit | adapters: request/response mapping, tool_choice, OpenAI image-split ordering, error normalization, cache usage | mocked HTTP (`respx`) |
| Unit | agent loop: nudges, forced submit, budget exceeded, self-correction, vision tool filtering | `FakeProvider` scripts |
| Unit | timeline math, cue splitting, ffmpeg argv and filter-script builders | pure functions |
| Integration | each scene type renders to 1920×1080 PNG; 3-scene compose passes ffprobe checks | real Chromium + ffmpeg, `say` audio |
| Replay E2E | whole pipeline from a recorded real transcript against a committed snapshot of the demo repo files the agent used | `FakeProvider` replay → real MP4 |
| Live E2E | real demo repo + real LLM; marker `live`, excluded by default | requires API key |

### 10.4 Eval (`codecast eval`)

Prints the §1 targets table from `run_report.json`, ffprobe and an **independent re-check** of the final storyboard (re-runs §7 rules on the committed `storyboard.json` against a fresh clone at `commit_sha`). Appends results to `docs/eval.md`. Clarity is a manual human rating recorded there.

### 10.5 run.yaml

```yaml
setup_commands:
  - "python3 -c \"import sys; assert sys.version_info >= (3, 12), 'Python 3.12+ required'\""
  - "python3 -m venv .venv"
  - ".venv/bin/python -m pip install --upgrade pip -q"
  - ".venv/bin/python -m pip install -e . -q"
  - ".venv/bin/python -m playwright install chromium"
  - ".venv/bin/codecast fetch-voice"
  - ".venv/bin/codecast doctor"

run_commands:
  - ".venv/bin/codecast run --repo https://github.com/rkumar-bengaluru/video-classifier --out output/"
  - "ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 output/video-classifier/explainer.mp4"

test_commands:
  - ".venv/bin/python -m pytest -q"

notes: "macOS. Requires ffmpeg >= 7 (brew install ffmpeg) and ANTHROPIC_API_KEY or OPENAI_API_KEY. TTS is local (piper, falling back to macOS say). Example output committed in output/video-classifier/."
```

### 10.6 Known risks (verify early)

- **piper on Python 3.14:** the default `python3` here is 3.14; piper needs onnxruntime wheels that may not exist for 3.14. Mitigation: pin the venv to 3.12, or fall back to `say`.
- **Cost:** < $0.10 with a fully agentic loop is unproven until the first live run; prompt caching and the step budget are the levers.
- **Strict grounding** (no derived numbers) may force more agent rounds; measured via `validation.failures_by_code`.

## 11. Phase B (out of scope)

GitHub Action that comments PRs with the video; private repos; robustness on unusual repos (monorepos, notebook-only); agent context summarization; 9:16 output; multi-language narration; crossfades; executing demo commands in a container for real terminal output; additional native providers.

## 12. Corrections to README.md

| README says | This spec |
|---|---|
| `edge-tts` is free and offline | It is online and unofficial; opt-in only. Default is local `piper`, fallback `say`. |
| Playwright records the terminal demo | Playwright renders all scenes from HTML; the terminal scene shows real commands from cited spans, never invented output. |
| `claude_agent.run(prompt, tools=...)`, `CLAUDE_KEY`, `claude -p` | Own agent loop over a provider-agnostic adapter; keys `ANTHROPIC_API_KEY` / `OPENAI_API_KEY`. |
| Timing: 3 acts 10/30/20 s; storyboard 50 s; compose 55 s | Scene durations come from measured TTS audio; total enforced to 50–60 s. |
| Script cites line numbers | Citations live in `storyboard.json` per sentence, not in the spoken narration. |
| YouTube Shorts ready | Phase A is 16:9 1920×1080; 9:16 is Phase B. |
| Captions burned in by ffmpeg | Captions rendered as PNGs and overlaid (no libass in the Homebrew ffmpeg build). |
| `src/*.py` flat layout | Installable package `src/codecast/`. |
| Mermaid CLI (`mmdc`) | `mermaid.js` vendored and run inside Playwright. |
