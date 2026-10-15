# CodeCast – Repo to 60-Second Developer Video

> Give CodeCast a GitHub repo; get a 50–60 second explainer video (MP4 + SRT + storyboard) whose every number and identifier is grounded in the repo's actual files.

## 1. Problem

Developer docs are walls of text. A new engineer joining a team that built a video understanding system needs 30 minutes to read the README, `run.yaml` and the results JSON. Pareto plots, trade-offs and configs are hard to grasp from text; Loom videos are manual, go stale in a week and are not grounded in code.

## 2. How it works

```
repo URL ─▶ [1] Ingest      shallow clone at HEAD → sandboxed, read-only file map
         ─▶ [2] Director    LLM agent with read-only tools (list_dir, read_file, grep, read_json, view_image)
                             explores the repo and submits a storyboard from a fixed scene catalog;
                             a code validator rejects any number/identifier not found in a cited source
         ─▶ [3] Voice       local TTS per sentence (piper, or macOS say) → measured timeline; total must be 50–60 s
         ─▶ [4] Render      Playwright renders every scene and caption from HTML templates (fonts and mermaid.js vendored)
         ─▶ [5] Compose     ffmpeg: per-scene segments + caption overlays + loudness-normalized narration
         ─▶ output/<repo>/  explainer.mp4 · explainer.srt · storyboard.json · run_report.json
```

- **Agent decides, code checks.** The LLM picks scenes and writes narration; it never writes HTML or ffmpeg commands, and it never judges groundedness — `codecast/grounding/` does.
- **Citations live in `storyboard.json`**, per sentence (file + line range, or JSON pointer). The spoken narration stays natural.
- **Fail loudly.** Every failure has a typed exit code (0 ok · 1 unhandled · 2 ingest · 3 duration · 4 agent budget · 5 validation · 6 render/compose · 7 config/preflight) and a `run_report.json`.

Design spec: [`docs/superpowers/specs/2026-10-15-codecast-design.md`](docs/superpowers/specs/2026-10-15-codecast-design.md).

## 3. Quick start (macOS)

```bash
brew install ffmpeg                       # ffmpeg >= 7
python3 -m venv .venv && .venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m playwright install chromium
.venv/bin/codecast fetch-voice            # piper voice, if piper is available; otherwise macOS say is used
.venv/bin/codecast doctor
export ANTHROPIC_API_KEY=...              # or OPENAI_API_KEY with a config file (below)
.venv/bin/codecast run --repo https://github.com/rkumar-bengaluru/video-classifier --out output/
.venv/bin/codecast eval output/video-classifier/
```

Other commands: `codecast run ... --from-stage voice|render|compose --run <run_id>` resumes a run from `workspace/<run_id>/`; `--allow-network-tts` enables `edge-tts` (online, unofficial) as a last-resort voice.

## 4. LLM providers

The LLM sits behind a provider-neutral adapter (`codecast/llm/`). Default: Anthropic `claude-sonnet-5-5`, temperature 0.3. To use OpenAI or any OpenAI-compatible endpoint (OpenRouter, Ollama, vLLM), write a config file and pass `--config`:

```yaml
llm:
  provider: openai
  model: <model id>            # required for non-Anthropic providers
  api_key_env: OPENAI_API_KEY
  base_url: null               # e.g. http://localhost:11434/v1 for Ollama
```

Cost is computed from the `pricing` table in `src/codecast/defaults.yaml` (rates copied from the providers' official pricing pages); an unknown model reports `cost_usd: null` with a reason instead of a guess.

## 5. Eval

| Metric | How | Target |
|---|---|---|
| Ungrounded numbers / identifiers (narration and on-screen) | code validator + independent re-check on a fresh clone | 0 |
| Duration | ffprobe | 50–60 s |
| Caption coverage of narration | SRT vs measured narration time | ≥ 95% |
| Cost per video | token usage × configured rates | < $0.10 (reported as measured) |
| End-to-end time | stage timers in `run_report.json` | < 5 min |
| Clarity | human rating 1–5, recorded in `docs/eval.md` | > 4 |

`codecast eval output/<repo>/` prints this table and appends it to [`docs/eval.md`](docs/eval.md).

## 6. Results

See [`docs/eval.md`](docs/eval.md) for measured results of the committed demo, `output/video-classifier/explainer.mp4`.

## 7. Layout

```
src/codecast/   cli · pipeline · config · models · report · preflight · eval
  ingest/       clone · repomap · sandbox
  llm/          neutral types · adapters (anthropic, openai, fake) · registry · pricing
  agent/        loop · tools · submit · transcript · prompts/director.md
  grounding/    numbers · spans · validator
  voice/        engines · timeline · fetch_voice
  render/       renderer · templates/ · static/ (vendored fonts, mermaid.min.js)
  compose/      segments · ffmpeg
tests/          unit · integration · e2e (offline by default; `-m live` needs an API key)
output/video-classifier/   committed demo
```

## 8. Phase B (not in this version)

GitHub Action that comments PRs with the video; private repos; monorepos and notebook-only repos; agent context summarization; 9:16 output; multi-language narration; crossfades; executing demo commands in a container for real terminal output; more native providers.

## 9. What changed from the original sketch

| Original sketch | Now |
|---|---|
| `edge-tts` "free, offline" | It is online and unofficial: opt-in only. Default is local piper, fallback macOS `say`. |
| Playwright records a terminal demo | Playwright renders all scenes from HTML; the terminal scene shows real commands from cited lines, never invented output. |
| `claude_agent.run`, `CLAUDE_KEY`, `claude -p` | Own agent loop over a provider-agnostic adapter; keys `ANTHROPIC_API_KEY` / `OPENAI_API_KEY`. |
| Fixed 3 acts (10/30/20 s) | Scene durations come from measured TTS audio; total enforced to 50–60 s. |
| Script cites line numbers aloud | Citations live in `storyboard.json` per sentence. |
| YouTube Shorts ready | 16:9 1920×1080 now; 9:16 is Phase B. |
| Captions burned in by ffmpeg | Captions rendered as PNGs and overlaid (Homebrew ffmpeg has no libass). |
| Flat `src/*.py` | Installable package `src/codecast/`. |
| Mermaid CLI (`mmdc`) | `mermaid.js` vendored and run inside Playwright. |
