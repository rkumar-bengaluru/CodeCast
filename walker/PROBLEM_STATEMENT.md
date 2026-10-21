# Walker: a living screenplay for every repo

## The problem

CodeCast turns a GitHub repo into a short narrated demo video. Today its director (an LLM with read-only tools) explores the repo cold on every run. It has no idea what the project is *for*, which parts matter, or what a viewer should come away knowing. So it opens whichever files look interesting, and the video feels like a random tour of the code rather than a demo of the product.

The real gap is that intent lives in people's heads, not in the repo. A README says what a project is. It rarely says:

- which parts exist (library, web UI, CLI, simulator, ...) and how they relate;
- what each part is worth showing, and in what order;
- how to run it so a viewer can see it working;
- what changed recently and is worth highlighting.

Recovering that by reading the code on every run is expensive, slow, unreliable, and different every time.

## The idea

Every repo carries a `.walker/` folder: a living, plain-language **screenplay** of the project.

A **walker** keeps it current as the code changes. It summarizes each part of the repo (library, web UI, etc.) and updates only what a change affects. Any LLM-driven tool (CodeCast first, but also code assistants and onboarding bots) reads `.walker/` to understand the project the way its authors do. It then builds a real demo from that understanding instead of from guesses.

## Goals

- **Intent first.** A demo follows the story the project wants told, not the files the model happened to open.
- **Always current.** The screenplay tracks the code. Every note records the commit it describes, so stale sections are detectable.
- **Cheap to consume.** A tool reads `.walker/` once instead of crawling the repo, which cuts tokens and run time.
- **Human-reviewable.** Changes to `.walker/` are small diffs a maintainer can approve or edit.
- **Tool-agnostic.** A plain, documented format that any LLM or tool can read, not something only CodeCast understands.

## Non-goals

- Replacing the README or the API docs.
- Generating documentation for end users.
- Becoming the source of truth for facts about the code (see the grounding constraint below).

## Anti-goals (failure even if it "works")

- The screenplay drifts from the code and the demo confidently shows things that no longer exist.
- Walker summaries become the evidence, so the video states things the repo does not actually contain.
- Maintainers have to hand-write and babysit a large folder of prose.
- Every merge produces a noisy, rewrite-everything PR that nobody reads.

## Hard constraint: grounding

CodeCast promises that a video never says anything the repo doesn't literally contain. Walker summaries are LLM-written, so they cannot be the evidence. They decide **what to show and in what order**. Every factual claim in `.walker/` carries a pointer (a file plus line range, a command, or a captured output) that the consumer re-verifies against the code before using it.

## Options considered

| | What it is | Strengths | Costs |
|---|---|---|---|
| **A. Curated screenplay** | A human writes `.walker/screenplay.md`: scenes, order, why each matters, pointers to files. | Cheap, dependable, fully intentional. | Goes stale unless someone maintains it. |
| **B. Walker bot** | A GitHub Action runs on each merge to `main`. It reads the diff, updates only the affected part notes and scenes, and opens a small PR for human approval. Notes record their commit SHA. | Stays current; humans stay in the loop; changes are incremental. | A few cents of LLM cost per merge; needs good diff-to-part mapping. |
| **C. B plus live "takes"** | The screenplay also holds runnable takes: a command plus its expected output, or a web UI page plus the clicks to make. A sandboxed runner (e.g. Docker plus Playwright) plays them and captures real screenshots and terminal output. | A true product demo of the app actually running, not a code tour. | Much heavier: services to start, test data, secrets, flaky UIs, sandbox security. |

**Current recommendation:** build **B**, and design the take format in from the start so **C** can follow without a format break.

## Sketch of the folder (to be designed)

```
.walker/
  walker.yaml          # format version, project one-liner, audience, parts list, last walked commit
  parts/
    library.md         # what it does, key files (with line ranges), how to use it, walked-at SHA
    web-ui.md
    cli.md
  screenplay.md        # the demo story: ordered scenes, each pointing at parts and evidence
  takes/               # (option C) runnable demo steps
    cli-quickstart.yaml
    web-ui-login.yaml
```

## Open questions

1. **What does "real demo" mean?** Is the goal to show the app *running* (web UI screens, real command output) or a guided tour of the code? This decides whether C is required or optional.
2. **Who maintains `.walker/` day to day?** Is it a bot with human review (B), humans only (A), or a mix where humans own the screenplay and the bot owns the part notes?
3. **Trigger.** Run on every merge to `main`, on every PR, on a schedule, or on demand (`walker walk`)?
4. **Format.** Markdown with front-matter (human-friendly) or structured YAML (machine-friendly), or Markdown for parts and YAML for takes?
5. **Repo boundaries.** Monorepos with several products: one screenplay or one per product?
6. **Staleness policy.** When a note's SHA is behind and the walker hasn't caught up, should consumers refuse, warn, or fall back to free exploration?
7. **Distribution.** A standalone tool or GitHub Action that CodeCast consumes, or a feature inside CodeCast?
8. **Secrets and safety (C).** How do takes get credentials and test data without leaking them into the screenplay or the video?

## First consumer: CodeCast

When `.walker/` exists, CodeCast's director starts from `screenplay.md` instead of free exploration. It follows the scene order, uses the part notes for narration intent, re-verifies every cited pointer through its existing grounding validator, and (with C) uses captured takes as visuals. When `.walker/` is absent or stale, CodeCast falls back to today's behavior.
