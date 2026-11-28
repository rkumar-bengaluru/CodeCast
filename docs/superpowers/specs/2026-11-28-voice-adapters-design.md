# Voice adapters: any engine behind one voice name

**Date:** 2026-11-28. **Status:** reviewed (architect review 2026-11-28, findings folded in). **For:** Walker's
backlog 15 (a voice library, every episode in English and Hindi), step 1. Walker's own steps (the library, the Hindi
cut) are specced in Walker; the one Walker change this spec needs is named in 5.10.

## 1. Why

CodeCast voices every video with one engine picked by a probe: piper, then macOS `say`, then edge-tts
(`src/codecast/voice/engines.py`, `select_engine`). The only choice a caller has is a custom piper `.onnx`
(`--voice-model`, `CODECAST_VOICE_MODEL`). Walker now wants a voice per series and per language, voiced by local
neural models (OmniVoice for Hindi, and possibly for English) on the Mac (MLX) and on Windows (PyTorch, NVIDIA GPU).

The maintainer's rule for this change: **what does the voicing, the model or the mechanism, can be swapped**: piper,
edge-tts, a local model, or something not yet thought of, all behind the same name and the same interface, without
CodeCast's pipeline knowing which.

An engine only Walker knew would not work: CodeCast picks its engine itself (`pipeline.py:108`) and re-voices in its
duration-feedback rounds, so a sentence Walker voiced with another engine would miss the cache and come out in
piper's voice.

## 2. What was measured (Walker backlog 15, 2026-11-28)

On an RTX 3060 Laptop GPU (6 GB), Performance mode, PyTorch 2.8 + CUDA 12.8, each model voicing narration
sentences from a saved reference clip:

| Model | Per sentence | Time ÷ speech | VRAM peak | Cold start (weights cached) |
|---|---|---|---|---|
| OmniVoice, `num_step=16` | 1.1 to 2.1 s | 0.34 | 3.35 GiB | about 25 s (16 s import, 4 to 8 s load, 3 s first call) |
| Qwen3-TTS 1.7B (Base) | 4 to 28 s | 4.3 | 4.3 GiB | about 21 s (9 s import, 8 s load, 4 s first call) |

Two consequences shape the design:

- **The cold start costs more than ten sentences.** edge-tts runs one process per sentence; a model engine built
  that way would be about ten times slower. A model must stay loaded for the whole run, in **one long-lived worker
  process**.
- **The models' dependencies are heavy and clash** (OmniVoice wants transformers 5, qwen-tts transformers 4.57;
  both pin torch builds per CUDA version; MLX is Mac only). They cannot live in CodeCast's environment, nor in one
  shared environment. Each worker runs in **its own Python environment**.

## 3. Goals, non-goals, anti-goals

**Goals**

1. One way to name a voice, `<engine>:<voice>`, for every engine: `piper:en_US-lessac-medium`, `say:Samantha`,
   `edge-tts:en-US-AriaNeural`, `model:<path to a voice file>`.
2. A **model engine**: a long-lived worker process in its own environment, spoken to over a small line protocol,
   so any local (or remote) model plugs in by writing a worker, not by changing CodeCast.
3. Workers shipped with CodeCast for **OmniVoice** on PyTorch (CUDA, MPS or CPU) and **mlx-audio** on the Mac; a
   command that sets up a worker's environment.
4. Voicing from a **reference clip**, so a designed voice stays the same voice from sentence to sentence and
   machine to machine; a command that **designs** that clip from a text description (Walker's library calls it).
5. The audio cache stays correct: anything that changes the sound changes the cache key, and the same sentence in
   the same voice gives the same audio.
6. A run with no voice named behaves **exactly as today** (probe order, `--voice-model`, `CODECAST_VOICE_MODEL`).

**Non-goals**

- Walker's voice library, its ids, `series.yaml`'s `voices`, and the Hindi screenplay: Walker's own specs.
- A Qwen3-TTS worker. It measured 4.3 times slower than speech and has no Hindi; with the protocol and `command:`
  it can be added later without changing CodeCast's core.
- Python entry-point plugins for engines. The worker protocol is the plug: anything that can read and write lines
  is an engine, in any language, in any environment.
- Streaming audio, several voices in one video, speed or pitch control beyond what a worker's `options` pass.
- A separate weights-download command: the model library downloads on first load; `codecast doctor --deep`
  (5.7) loads the model once, which is the way to download ahead of a render.

**Anti-goals** (would make it a failure even if it works)

- Heavy dependencies (torch, transformers, mlx) in CodeCast's own requirements.
- A named voice that silently falls back to another engine or voice.
- A worker left running, holding GPU memory, after a run ends, fails or is killed.
- A slower render for the common case: a run whose sentences are all in the cache must not start a worker.
- Secrets in a worker's environment.

## 4. Approaches considered

1. **In-process engines with optional extras** (`pip install codecast[omnivoice]`, imported like piper). Simple, but
   the models' dependencies clash with each other and with CodeCast's, and a CUDA torch in CodeCast's venv ties
   CodeCast to one GPU stack. Rejected.
2. **One process per sentence** (as edge-tts). Isolates dependencies, but pays the 20 to 25 s cold start on every
   sentence. Rejected by the measurement.
3. **A long-lived worker per run, in its own environment, over a line protocol** (chosen). Isolates dependencies,
   pays the cold start once, and makes "swap the mechanism" literal: a worker is any program that speaks the
   protocol. Costs a protocol, process management and an environment per worker.
4. **A local HTTP server per model.** Same isolation as 3, but adds ports, a server lifecycle the user must manage,
   and a race with other services. Pipes give the same thing with the process tied to the run. Rejected; a worker may
   still forward to a server if someone wants that.

## 5. Design

### 5.1 Naming a voice

`VoiceConfig` gains one field, `voice: str | None`, `<engine>:<voice>`. Two settings can now choose a voice,
`voice` and the existing piper `model_path`, each from three layers, strongest first:

1. the flag: `codecast run|doctor --voice <engine>:<voice>` (or `--voice-model`);
2. the environment (or `.env`): `CODECAST_VOICE` (or `CODECAST_VOICE_MODEL`);
3. the config file: `voice.voice` (or `voice.model_path`).

**The strongest layer that sets either one wins**, across both settings; both set **in the same layer** is a
`ConfigError` (two choices, neither silently wins). `load_config` records which layer each came from so the rule
can be applied, and the rule lives in config resolution, not in `cli.py`, so the in-process path (Walker) and the
CLI behave the same. `model_path` set alone means `piper:<path>`, as today.

The engine part names an engine in CodeCast's registry: `piper`, `say`, `edge-tts`, `model`. The voice part is the
engine's own: a piper voice name or `.onnx` path, a `say` voice, an edge-tts voice, or a voice file path for
`model`. Paths are resolved against the current directory when the config is loaded, and kept absolute from then
on. An unknown engine, or a voice part the engine cannot find, is a `ConfigError` (exit 7) naming what is missing.
**A named voice never falls back.** `edge-tts:` still needs `--allow-network-tts`: naming it does not grant network
access.

With neither setting, everything is as today: the probe picks piper, then `say`, then edge-tts.

**Security note:** a voice file can name a `command:` (5.3), and the voice can come from a `.env` file, so
whoever writes the `.env` (or the voice file) can run a program. This is the same trust CodeCast already gives
`.env`; the doctor prints the command a voice will run.

### 5.2 The engine interface and registry

`TTSEngine` keeps `name`, `voice_id` and `synthesize(text, out_wav)` and gains `close()`, a no-op for the built-in
engines. Every engine writes a 48 kHz mono 16-bit WAV, as now (`convert_to_wav48k`).

`select_engine(cfg, allow_network=...)` keeps its signature and return type, so existing callers keep working. It
resolves the voice through a registry: a dict from engine name to a small class with `status(voice_part, cfg) ->
str` ("ok" or the problem) and `create(voice_part, cfg) -> TTSEngine`. `engine_status` returns the four engines'
status; preflight reports the named engine's status, or the probe's choice as now.

**Whoever creates an engine closes it**: `with contextlib.closing(select_engine(...))`. The pipeline closes the
engine it created in its existing `finally` (`pipeline.py:227`), but not one passed in by its caller
(`run(engine=...)`); `estimate_length` likewise. A caller that forgets is covered by 5.4's guarantees, but it holds
the worker (and its GPU memory) until it exits, so for Walker closing is required (5.10).

### 5.3 The model engine and its voice file

`model:<path>` names a **voice file**, YAML:

```yaml
# hi-narrator-warm.yaml
worker: omnivoice             # a worker shipped with CodeCast, or `command:` (below)
model: k2-fsa/OmniVoice
language: hi                  # passed to the worker; not interpreted by CodeCast
device: auto                  # auto | cuda | cuda:N | mps | cpu; auto = cuda, else mps, else cpu
reference: hi-narrator-warm.wav       # relative to this file
reference_text: "नमस्ते, मैं इस श्रृंखला में आपका मार्गदर्शन करूँगी।"
options: { num_step: 16 }     # passed to the worker as they are
platforms:                    # optional: per-platform overrides of worker, model, device, options
  darwin: { worker: mlx-audio, model: mlx-community/OmniVoice-bf16 }
```

- `worker` names a shipped worker (5.6). Instead, `command: [argv...]` runs any program that speaks the protocol,
  spawned with that argv list (never a shell), `{voice_dir}` replaced by the voice file's folder.
- `platforms` keys are `sys.platform` values (`darwin`, `win32`, `linux`); an entry replaces those keys for that
  platform. So one voice file, and one reference clip, serves the Mac and Windows; they sound very close, not
  sample-identical, and their `voice_id`s differ.
- `reference` and `reference_text` are optional: without them the worker voices with the model's default voice (or
  an `instruct` in `options`, for models that design per call; the voice then drifts per sentence, which the doctor
  warns about).
- Unknown keys are an error (a typo must not silently change the voice).

**`voice_id`** (part of the audio cache key) is `<file stem>-<12 hex>`, the hex a SHA-256 over a canonical JSON
(`sort_keys`, NFC-normalised strings) of the voice **after** the platform overrides are applied: worker name (or the
`command` argv), `model`, `language`, `device` as given, `options`, `reference_text`, the **bytes** of the reference
clip, and, for a shipped worker, the bytes of its `<name>.py` and `<name>.requirements.txt`. So a changed clip,
setting or a CodeCast upgrade that changes the worker is a new voice; moving the voice file is not (its stem is part of the
id, so a rename changes it). A `command:` voice's own script is not hashed; to re-voice after changing it, change its argv
or options. The
model library's own version inside an environment the user upgraded by hand is not in the key; the run report records
it (5.4).

**Same sentence, same audio.** The models sample, so each `synthesize` carries a `seed`, the first 8 bytes of
SHA-256 over the NFC sentence text, as an unsigned integer; workers seed their libraries with it. A re-voiced
sentence then comes back with the same audio and the same length (up to GPU non-determinism, which is small).

### 5.4 The worker's life

- **Started lazily**, on the first `synthesize` call. A run whose sentences are all in the audio cache (Walker's
  render after Walker voiced the storyboard) never starts a worker and pays no cold start.
- **One worker per engine object, for the engine's life**: every sentence and every duration round of a run go to
  the same process.
- **Spawned clean**: an argv list; the environment is an **allowlist**, not CodeCast's whole environment: `PATH`,
  `SYSTEMROOT`, `WINDIR`, `TEMP`, `TMP`, `HOME`, `USERPROFILE`, `LOCALAPPDATA`, `APPDATA`, `HF_*`, `HUGGINGFACE_*`,
  `TRANSFORMERS_*`, `CUDA_*`, `PYTORCH_*`, `TORCH_*`, `NVIDIA_*`, plus `PYTHONUTF8=1`, `PYTHONIOENCODING=utf-8` and
  `PYTHONUNBUFFERED=1`. API keys and the rest of `.env` never reach a worker.
- **Pipes are drained**: one CodeCast thread reads the worker's stderr into `run.log` and keeps its last 40 lines;
  one reads stdout into a queue, so every wait has a timeout on Windows too (no `select` on pipes there). A stdout
  line that is not JSON is logged and ignored.
- **Startup is reported and bounded.** The first load may download gigabytes, so the startup timeout is long
  (`voice.worker_start_timeout_s`, default 1800) and the worker sends `progress` lines that CodeCast logs (and
  prints with `--verbose`). A sentence that takes longer than `voice.worker_sentence_timeout_s` (default 300) kills
  the worker and fails the voice stage.
- **Stopped** by `close()`: send `quit` (the worker finishes the sentence in hand, then exits), wait 10 s, then kill
  the worker **and its children**.
- **Never orphaned**:
  - the worker watches stdin on its own thread and calls `os._exit` on EOF, so it ends even while it is loading or
    generating, when CodeCast dies;
  - on Windows CodeCast puts the worker in a Job Object with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`, so killing
    CodeCast (Task Manager, a closed console) kills the worker and anything it started (a `command:` wrapper's
    grandchildren);
  - on POSIX the worker starts in its own session (`start_new_session=True`) and is killed by process group. Ctrl+C
    then no longer reaches it, so the pipeline closes the engine on `KeyboardInterrupt` (its `except BaseException`,
    `pipeline.py:212`, already catches it);
  - an `atexit` handler closes engines a caller forgot.
- **One GPU, one worker at a time.** A worker on a GPU device takes a lock file
  (`<envs_dir>/locks/<device>.lock`, holding the pid). A second run waits for it, logging "waiting for the voice
  worker of pid N (another CodeCast run)", up to the startup timeout. A CUDA out-of-memory error from a worker is
  reported as such, with that hint.
- **Failures** are voice-stage `CodecastError`s carrying the worker's error message and the last 40 lines of its
  stderr. A worker that exits unexpectedly is not restarted: the run fails, naming the sentence.
- The run report's `tts_engine` / `tts_voice` are `model` and the `voice_id`; a new `tts_worker` records the
  worker's `ready` reply (worker name, model, device, library versions).

### 5.5 The protocol

JSON, **ASCII only** (`ensure_ascii=True` both ways, so no code page can garble Hindi), one object per line. CodeCast
writes requests to the worker's stdin; the worker writes replies to its protocol stream. A shipped worker keeps the
real stdout for the protocol (`proto = os.dup(1)`) and points fd 1 at stderr (`os.dup2(2, 1)`) **before importing
any model library**, so nothing a library (or native code) prints can corrupt the protocol. One request at a time;
each reply repeats its request's `id`.

```text
→ {"id":1,"op":"hello","protocol":1,"voice":{...the resolved voice, reference as an absolute path...}}
← {"id":1,"progress":"loading k2-fsa/OmniVoice on cuda:0"}            (any number, optional)
← {"id":1,"ok":true,"ready":{"worker":"omnivoice","model":"k2-fsa/OmniVoice","device":"cuda:0",
                             "versions":{"omnivoice":"0.2.1","torch":"2.8.0+cu128"}}}
→ {"id":2,"op":"synthesize","text":"…","seed":1234567,"out":"C:/…/abc.raw.wav"}
← {"id":2,"ok":true}
→ {"id":3,"op":"design","text":"…","language":"hi","instruct":"female, warm, mid pitch","seed":42,"out":"…/ref.wav"}
← {"id":3,"ok":true}
→ {"id":4,"op":"quit"}
← {"id":4,"ok":true}
   any request may get {"id":n,"ok":false,"error":"…"}
```

- `hello` loads the model and, when the voice has a reference, prepares it once (OmniVoice's
  `create_voice_clone_prompt`), so each sentence pays only generation.
- `synthesize` writes a WAV at the model's own rate; CodeCast converts it to 48 kHz (`convert_to_wav48k`) and
  measures it itself.
- `design` makes a reference clip from a description, ignoring any reference in the voice; it is how a voice is made
  in the first place (5.8). A worker that cannot design answers `ok:false`.
- `protocol` is 1; a worker that does not speak the version it is sent answers `ok:false` and exits.

### 5.6 Shipped workers and their environments

Workers ship as package data, `src/codecast/voice/workers/<name>.py`, each with a pinned `<name>.requirements.txt`.
A worker imports only the standard library and its model's packages, **never `codecast`** (its environment does
not have it); the protocol side (stream setup, stdin watcher, request loop) is one stdlib file beside them,
`_protocol.py`, which a worker imports from its own folder.

| Worker | Model packages | Platforms | Models |
|---|---|---|---|
| `omnivoice` | `omnivoice`, `torch`, `soundfile` | Windows, Linux (CUDA or CPU), Mac (MPS) | `k2-fsa/OmniVoice` |
| `mlx-audio` | `mlx-audio` | Mac (Apple silicon) | `mlx-community/OmniVoice-bf16` and other mlx-audio TTS models |

**Environments**: `codecast voice-env <worker> [--torch cu128|cpu]` creates `<voice.envs_dir>/<worker>/` (default
`~/.cache/codecast/envs/`) with `uv venv --python 3.12` and installs the worker's requirements (torch from the
PyTorch index matching `--torch`; default `cu128` when `nvidia-smi` runs, else `cpu`; the Mac's default PyPI torch
has MPS). It needs `uv` on PATH and says so if missing, and takes `<envs_dir>/locks/env-<worker>.lock` so two runs
cannot build one environment at once. The model engine finds a worker's interpreter at `<envs_dir>/<worker>/`
(`Scripts/python.exe` on Windows, `bin/python` elsewhere), or at `voice.worker_python.<worker>` in the config when
the environment lives elsewhere.

### 5.7 Doctor

`status` for `model:` checks, without starting the worker: the voice file parses, the reference exists, the worker
is known (or the command's program exists), and its interpreter exists. It prints the command the voice will run.
`codecast doctor --voice model:<file> --deep` also starts the worker, sends `hello`, and reports the `ready` reply
and how long it took (which downloads the weights the first time). On Windows the doctor warns when Developer Mode
is off: the Hugging Face cache then copies files instead of linking them (more disk, nothing else).

### 5.8 Designing a voice

`codecast voice-design <new voice file> --worker omnivoice --model k2-fsa/OmniVoice --language hi
--instruct "<description>" --text "<a sentence or two>" [--option k=v ...] [--seed N]` starts the worker, asks it to
`design`, and writes **both** the clip (`<stem>.wav` beside the file) and the whole voice file, with `reference` and
`reference_text` filled in; it refuses to overwrite either without `--force` (so no YAML is rewritten in place). It
prints the new `voice_id`. Walker's library drives this command (or the same function,
`codecast.voice.design.design_voice`).

### 5.9 The audio cache across rounds

Today each duration round voices into a fresh `ws/audio/round{n}` (`pipeline.py:158`, `:179`), so a revision round
re-voices sentences it did not change, and `estimate_length` voices into `audio/estimate` (`estimate.py:38`) even
when round0 holds every sentence. With a model engine that costs seconds per sentence and a cold start. So
`synthesize_storyboard`, before voicing a sentence, looks for its key in the sibling folders of `cache_dir` under
the same `audio/` and copies the WAV it finds. The folders, file names and key formula stay as they are, so Walker's
pre-filled `round0` keeps working.

### 5.10 The Walker seam

Walker changes nothing until it names a voice. When it does (Walker's own spec, step 2 of backlog 15):

- `narration_seconds` (`walker/render/run.py:49-70`) must close its engine
  (`with closing(select_engine(...))`) before filming, or its worker holds the GPU through the render and CodeCast's
  own engine cannot start one.
- It must name the same voice in both calls: `cfg.voice.voice` in-process and `--voice` to `cli.main`
  (`run.py:169-172`), or the cache misses.
- Walker's `voice_model` keeps working as `piper:<path>` through `--voice-model`.

### 5.11 What stays the same

- piper, `say` and edge-tts behave as now, under their new names too; `fetch-voice` still fetches piper's voice.
- The audio cache's folders, file names and key formula (`engine.name`, `voice_id`, sentence); only the model
  engine's `voice_id` is new.
- One engine per video, chosen at startup.
- Measured WAV duration is the source of truth; no time stretching.

## 6. Testing

- **Protocol and process management** with a fake worker (a stdlib script that writes a sine wave, and can be
  told to be slow, crash, print to stdout and to fd 1, flood stderr, ignore `quit`, or answer `ok:false`): lazy
  start, one process across rounds, `close`, stdin EOF while busy, timeouts, kill of children, stderr in the error,
  no worker on an all-cache run, the GPU lock, Hindi text through the pipe on Windows.
- **Naming**: the precedence and same-layer rule across `voice` and `model_path`, in-process and through the CLI;
  unknown engine; no fallback; `edge-tts:` without `--allow-network-tts`.
- **`voice_id`**: stable across moves; changed by the clip's bytes, each setting, a platform override and the
  worker's files; the same for equal `options` in a different order.
- **The environment allowlist**: a key set in CodeCast's environment is absent in the worker's.
- **Round cache**: an unchanged sentence in round 1 is copied, not voiced.
- **Real workers** behind a `gpu` marker (excluded by default, like `live`): `omnivoice` voices one sentence from a
  reference, twice with the same seed (same samples), and designs one clip; run by hand on the Windows GPU, and
  `mlx-audio` on the Mac.

## 7. Left to the builder

- The registry's shape beyond `status` and `create`, and module layout under `codecast/voice/`.
- How `voice-env` passes the torch index to `uv pip install`, and the pinned versions (start from the measured
  `omnivoice 0.2.1`, `torch 2.8.0+cu128`).
- How the Job Object is made (ctypes or `pywin32`; ctypes keeps the dependency out).
- Exact log lines and the doctor's wording.
