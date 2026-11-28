# Mac checklist: voice adapters (1.2.0)

The voice adapters (spec `docs/superpowers/specs/2026-11-28-voice-adapters-design.md`) were built and tested on
Windows 11 with an RTX 3060. The POSIX paths and the mlx-audio worker have not run yet. Run this on an M-series Mac
before tagging `v1.2.0`. Tick each item and note what you saw.

## Setup

- [ ] Pull `main`, then reinstall into the venv (`uv pip install -e '.[dev]'`).

## Unit tests

- [ ] The full unit suite passes completely. The 19 tests that fail on Windows fail only there (symlinks, cp1252,
  ffmpeg), so any failure on the Mac is real.
- [ ] In `tests/unit/test_worker_client.py`: `test_process_group_kills_children` and the `fcntl` lock tests (a second
  run waits for the lock, the wait is bounded, a hard-killed holder frees it).

## OmniVoice on PyTorch (MPS)

- [ ] `codecast voice-env omnivoice` installs `torch==2.8.0` and `torchaudio` from PyPI, and `omnivoice==0.2.1`
  installs without changing torch.
- [ ] `pytest -m gpu tests/gpu/test_omnivoice.py` passes. The test uses `device: auto`, so it should run on `mps` with
  float32. The first run downloads the weights.
- [ ] The same seed gives identical samples on MPS. If it doesn't, note it: the cache assumes the same text and seed
  give the same audio.

## mlx-audio (`mlx_audio_worker.py`, never run)

- [ ] `codecast voice-env mlx-audio` works. Does `mlx-audio==0.6.8` exist, does it load
  `mlx-community/OmniVoice-bf16`, and does it need an extra?
- [ ] `pytest -m gpu tests/gpu/test_mlx_audio.py` passes. If it fails, check these first:
  - `mlx_audio.tts.utils.load_model` is the right import;
  - the keyword names: `lang_code` or `language`, whether `ref_audio` is a path or an array, `ref_text`, and
    `instruct` for design;
  - `result.audio` and `result.sample_rate` exist;
  - `mx.random.seed` accepts seeds up to 2^64-1;
  - the same seed gives identical samples.
- [ ] The ready reply's `device` reads sensibly in `run_report.json` (`tts_worker`).

## Processes and the GPU lock

- [ ] `kill -9` CodeCast while the worker is loading, then again while it voices a sentence. Each time:
  - the worker exits, because it sees EOF on stdin;
  - the GPU memory is freed;
  - `~/.cache/codecast/envs/locks/gpu.lock` can be taken again at once.
- [ ] Ctrl+C during a `codecast run` voice stage, during `codecast voice-design` and during
  `--compute-video-length`: no stray python is left in `ps`.
- [ ] A `command:` voice whose wrapper starts a child process: after the run, `killpg` has taken the grandchild too.

## Voices and the doctor

- [ ] A voice file with a `platforms.darwin` override:
  - its `voice_id` differs from the Windows one;
  - the voice sounds the same;
  - the same reference clip is shared.
- [ ] The worker's environment on POSIX: the model loads and writes its temp files, and the Hugging Face cache is
  `~/.cache/huggingface`.
- [ ] `codecast doctor --voice model:<file> --deep` prints the worker's command and the ready reply with the time taken.
- [ ] `codecast doctor` with no voice named still lets `say -v ?` drive the probe, and its messages are as before.

## Walker

- [ ] In Walker's venv, `uv pip install --python .venv/bin/python -e ../CodeCast`, then `walker render` a golden
  example with no voice named. Nothing should change: piper or `say` is picked, the round0 cache hits, and no worker
  starts.

## When it passes

Tag `v1.2.0` and move Walker's CodeCast pin to it. Fix anything that failed on a branch first, especially
`mlx_audio_worker.py`, and add a line on what changed to the spec.
