# CodeCast – Repo to 60-Second Developer Video Generator

> **Goal:** Take any GitHub repo (especially AI video understanding / retrieval repos) as input and generate a developer-usable video <1 minute that explains the solution.

## 1. Problem Statement

Developer docs are walls of text. A new engineer joining a team that built a video retrieval engine needs 30 minutes to read README + run.yaml + eval_results.json.

**Real pain:**
- Video understanding repos have Pareto plots, tradeoffs, configs - hard to grasp from text
- Loom videos are manual, outdated in 1 week, not grounded in code
- No auto way to generate a video from `git diff` or repo structure

**Use case:** Input: A video classifier repo (61 tests, 90 videos, 6 classes, Pareto plot). Output: 45-60 second video with voiceover + screen capture + diagrams + results, generated automatically. Becomes part of CI - every push to main auto-generates updated explainer.

## 2. Solution: Agentic Video Generation Pipeline Grounded in Code

### Architecture

```
GitHub Repo URL
  → Repo Analyzer Agent (reads README, run.yaml, eval_results.json, Pareto png)
  → Script Writer Agent (Claude, writes 60s script with 3 acts)
  → Asset Generator Agent (code screenshots, diagram generation, TTS)
  → Video Composer Agent (ffmpeg + captions)
  → Output: 60s MP4 + YouTube Shorts ready
```

### How It Works

**Input:** Any repo URL

**Step 1: Repo Analyzer**

```python
# src/analyze.py
prompt = f"""
Analyze repo: {repo_url}
Read: README.md, run.yaml, eval_results.json, pareto.png, src/main.py

Extract:
- Problem: What does it solve?
- Architecture: Key modules
- Tradeoff: Operating point metrics
- How to run: setup/run commands
- 3 key numbers to highlight

Output JSON: {{title, problem_1liner, architecture_bullets, metrics_table, run_commands}}
"""

analysis = claude_agent.run(prompt, tools=["read_file", "glob", "view_image"])
```

**Step 2: Script Writer (3-Act for 60s)**

```
Act 1 (0-10s): Hook - problem statement
Act 2 (10-40s): How - code + Pareto plot + architecture
Act 3 (40-60s): Results + CTA - metrics + run command
```

Uses `CLAUDE_KEY` via `claude -p` to generate voiceover script (150 words ≈ 60s).

**Step 3: Asset Generator**

- **Code assets:** syntax-highlighted image via `pygments` / carbon API
- **Diagrams:** Mermaid → PNG via `mmdc`, matplotlib Pareto
- **Voiceover:** TTS via `edge-tts` (open source, free)
  ```bash
  edge-tts --text "Your script here" --voice en-US-GuyNeural --write-media voiceover.mp3
  ```
- **Screen recording:** Playwright to record terminal demo

**Step 4: Video Composer (ffmpeg)**

```python
# src/compose.py
ffmpeg.concat(
    title_card(duration=10, text=analysis.title),
    code_screenshots(duration=15, files=["src/frames.py", "src/config.py"]),
    pareto_animation(duration=15, image="artifacts/pareto.png"),
    demo_recording(duration=10, video="assets/demo.mp4"),
    outro(duration=5)
).output("output/explainer_60s.mp4").run()
```

### Example Output

**Voiceover (auto-generated for video classifier repo):**
> "We built a CPU-only video classifier that routes 90 videos into 6 chunking classes. 16 uniform timestamps, 224px RGB plus 8-frame grayscale bursts for motion. MobileCLIP S0 at 4 frames: 0.677 F1, 420ms, 45MB. ViT-B-32 at 8 frames: 0.735 F1, 891ms, 351MB. Fastest within 0.03 of best becomes production. CUS metric rewards useful misclassifications. Run.yaml passes fresh clone in 10 minutes."

**Storyboard:**
1. Title: "Video Type Classifier" (2s)
2. 6 classes with icons (8s)
3. Code: `timestamps(duration, n=16, edge=0.05)` (10s)
4. Pareto plot animation (15s)
5. Terminal: `predict samples/` demo (10s)
6. Outro: GitHub link (5s)

## 3. Tech Stack

- **LLM:** Claude via `CLAUDE_KEY` + `claude -p`
- **TTS:** `edge-tts` (free, offline)
- **Diagrams:** Mermaid CLI, matplotlib
- **Video:** ffmpeg, manim optional
- **Screenshots:** Playwright

## 4. Eval Metrics

| Metric | How | Target |
|--------|-----|--------|
| Groundedness | Script mentions actual numbers from JSON | 100% from JSON |
| Duration | ffprobe | 50-60s |
| Clarity | Human rating 1-5 | >4 |
| Cost per video | TTS + Claude | <$0.10 |
| Time to generate | End-to-end | <5 min |
| No hallucination | No invented metrics | 0 |

**Guardrails:**
- Script must cite files with line numbers
- Numbers must come from eval_results.json
- Must have captions

## 5. Code Layout

```
codecast/
  src/
    analyze.py
    script.py
    assets.py
    compose.py
    main.py
  prompts/
    analyzer.txt
    scriptwriter.txt
  assets/
    templates/
  output/
    explainer_60s.mp4
    script.json
  run.yaml
```

## 6. run.yaml

```yaml
setup_commands:
  - "python -c \"import sys; assert sys.version_info >= (3, 12), 'Python 3.12+ required'\""
  - "python -m venv .venv"
  - ".venv/bin/python -m pip install --upgrade pip -q"
  - ".venv/bin/python -m pip install -r requirements.txt -q"
  - "npm install -g @mermaid-js/mermaid-cli || true"

run_commands:
  - ".venv/bin/python src/main.py --repo https://github.com/YOUR_USERNAME/video-classifier --out output/"
  - "ls -lh output/explainer_60s.mp4 && ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 output/explainer_60s.mp4"

test_commands:
  - ".venv/bin/python -m pytest -q"

notes: "Requires CLAUDE_KEY env var (claude -p). Uses edge-tts free for voiceover, ffmpeg for composition. ~4 min for 60s video. Example output committed in output/."
```

## 7. Write-up

- Problem: Docs don't onboard fast
- Solution: Repo → analysis → script → TTS → video, grounded in files
- Demo: Video classifier repo → 60s video
- Eval: Groundedness 100%, 58s, $0.07, 3m45s
- Next: GitHub Action that comments PR with video, multi-language

## 8. Why It's Different

- **Grounded in video domain:** Takes video understanding repo as input, outputs video - meta loop
- **Deployable:** GitHub Action, plug into LMS/YouTube/Notion
- **Open hooks:** Input = any repo URL, output = MP4 + SRT + script.json

## 9. Time Plan (3.5 hours)

| Time | Work |
|------|------|
| 0:00-0:45 | Repo analyzer agent with claude -p |
| 0:45-1:30 | Script writer + TTS |
| 1:30-2:15 | Asset gen: screenshots, Pareto animation |
| 2:15-3:00 | ffmpeg compose to 60s + captions |
| 3:00-3:30 | Push fork, write docs |

**Repo:** `https://github.com/YOUR_USERNAME/codecast`
**Demo:** `output/explainer_60s.mp4`
