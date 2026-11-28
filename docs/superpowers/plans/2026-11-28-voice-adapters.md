# Voice adapters Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Any TTS engine (piper, say, edge-tts, or a local model in its own environment) behind one voice name,
`<engine>:<voice>`, with local models run as long-lived worker processes over a JSON-lines pipe.

**Architecture:** An engine registry resolves `cfg.voice.voice` (or today's probe). Built-in engines stay in-process;
the `model` engine reads a voice file, computes a `voice_id`, and lazily starts a worker (`codecast/voice/workers/`,
stdlib protocol + model packages, in a uv venv) that it keeps for the engine's life and kills reliably.

**Tech Stack:** Python 3.12, pydantic 2, PyYAML, subprocess + threads, ctypes (Windows Job Object), uv (for
`voice-env`), OmniVoice / mlx-audio in the workers only.

**Spec:** `docs/superpowers/specs/2026-11-28-voice-adapters-design.md` (read it with this plan).

## Global Constraints

- No new dependency in CodeCast's `pyproject.toml` `dependencies` (torch, transformers, mlx, pywin32 stay out).
- Files under `src/codecast/voice/workers/` import only the standard library and their model's packages, never
  `codecast`.
- A run with no voice named behaves exactly as today; every existing test passes unchanged.
- A named voice never falls back. Errors in naming or setup are `ConfigError` (exit 7); voicing failures are
  `CodecastError(stage="voice")`.
- Protocol JSON is `ensure_ascii=True` both ways, one object per line, `protocol: 1`.
- Defaults: `voice.worker_start_timeout_s: 1800`, `voice.worker_sentence_timeout_s: 300`, `voice.envs_dir:
  ~/.cache/codecast/envs`, close waits 10 s before a kill, 40 stderr lines kept.
- Worker env allowlist (spec 5.4): `PATH SYSTEMROOT WINDIR TEMP TMP HOME USERPROFILE LOCALAPPDATA APPDATA` and
  prefixes `HF_ HUGGINGFACE_ TRANSFORMERS_ CUDA_ PYTORCH_ TORCH_ NVIDIA_`, plus `PYTHONUTF8=1
  PYTHONIOENCODING=utf-8 PYTHONUNBUFFERED=1`.
- Tests: `.venv/Scripts/python -m pytest -q` (Windows) / `.venv/bin/python -m pytest -q`; new real-model tests carry
  a `gpu` marker added to `addopts` exclusion (`-m 'not live and not gpu'`).

## Review Focus

1. A worker that floods stderr during a long load (HF progress bars) must not block: covered in Task 5
   (`test_stderr_flood_does_not_block`).
2. Hindi (non-ASCII) text and a non-ASCII / spaced path through the pipe on Windows: Task 5
   (`test_hindi_text_and_spaced_path_round_trip`).
3. CodeCast killed mid-sentence leaves no worker: Task 5 (`test_worker_exits_on_stdin_eof_while_busy`; Job Object
   path asserted on Windows by `test_job_object_kills_children`).
4. Walker's in-process path and the CLI resolve the same voice and the same conflicts: Task 1
   (`test_conflict_rule_same_in_process_and_cli`).
5. A render whose sentences are all cached never starts a worker, including `estimate_length` after round0: Tasks 5
   and 6 (`test_all_cached_run_starts_no_worker`, `test_estimate_reuses_round0`).

---

### Task 1: Naming a voice (config, environment, flag)

**Files:**
- Modify: `src/codecast/config.py` (`VoiceConfig`, `load_config`), `src/codecast/settings.py`, `src/codecast/cli.py`,
  `src/codecast/defaults.yaml`
- Test: `tests/unit/test_config.py`, `tests/unit/test_cli.py`

**Interfaces:**
- Produces: `VoiceConfig.voice: str | None`; `VoiceConfig.voice_source: Literal["flag","env","config"] | None` and
  `VoiceConfig.model_path_source` (same type), excluded from dumps; `VoiceConfig.worker_start_timeout_s: float =
  1800`, `worker_sentence_timeout_s: float = 300`, `envs_dir: str = "~/.cache/codecast/envs"`,
  `worker_python: dict[str, str] = {}`; `settings.VOICE_VAR = "CODECAST_VOICE"`, `voice_from_env(env) -> str | None`;
  `config.apply_voice_flags(cfg: Config, *, voice: str | None, voice_model: str | None) -> None`;
  `config.resolved_voice(vc: VoiceConfig) -> str | None` returning the winning `<engine>:<voice>` (`model_path`
  alone becomes `piper:<abs path>`) or `None` for the probe, raising `ConfigError` on a same-layer conflict.

- [ ] **Step 1: Write the failing tests**

```python
def test_voice_from_env_and_config(tmp_path):
    cfg = load_config(write_yaml(tmp_path, {"voice": {"voice": "say:Samantha"}}), env={})
    assert resolved_voice(cfg.voice) == "say:Samantha"
    cfg = load_config(write_yaml(tmp_path, {"voice": {"voice": "say:Samantha"}}), env={"CODECAST_VOICE": "say:Alex"})
    assert resolved_voice(cfg.voice) == "say:Alex"            # env beats config file

def test_strongest_layer_wins_across_settings(tmp_path):
    cfg = load_config(write_yaml(tmp_path, {"voice": {"voice": "say:Samantha"}}),
                      env={"CODECAST_VOICE_MODEL": str(tmp_path / "v.onnx")})
    assert resolved_voice(cfg.voice) == f"piper:{tmp_path / 'v.onnx'}"

def test_same_layer_conflict_is_error(tmp_path):
    cfg = load_config(None, env={"CODECAST_VOICE": "say:Alex", "CODECAST_VOICE_MODEL": "x.onnx"})
    with pytest.raises(ConfigError, match="CODECAST_VOICE.*CODECAST_VOICE_MODEL"):
        resolved_voice(cfg.voice)

def test_model_voice_path_made_absolute(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = load_config(None, env={"CODECAST_VOICE": "model:voices/hi.yaml"})
    assert resolved_voice(cfg.voice) == f"model:{tmp_path / 'voices' / 'hi.yaml'}"

def test_no_voice_means_probe():
    assert resolved_voice(load_config(None, env={}).voice) is None
```

In `test_cli.py`: `test_voice_flag_beats_env` (flag `--voice say:Alex` with `CODECAST_VOICE=say:Bob` resolves to
`say:Alex`), `test_voice_and_voice_model_flags_conflict` (exit 7, message names both flags), and
`test_conflict_rule_same_in_process_and_cli` (the same env conflict raises from `resolved_voice` in-process and
gives exit 7 through `cli.main(["doctor"])`).

- [ ] **Step 2: Run them, expect FAIL** — `pytest tests/unit/test_config.py tests/unit/test_cli.py -q`
- [ ] **Step 3: Implement.** `load_config` records each setting's source (config file, then env overrides it);
  `apply_voice_flags` sets the flag layer; `resolved_voice` applies spec 5.1 (strongest layer across both; both in
  one layer → `ConfigError` naming the two settings by their user-facing names: `--voice`/`--voice-model`,
  `CODECAST_VOICE`/`CODECAST_VOICE_MODEL`, `voice.voice`/`voice.model_path`). Paths in `piper:`/`model:` voice parts
  that look like paths (contain a separator or end in `.onnx`/`.yaml`/`.yml`) become absolute against the cwd at
  resolution. `cli.py` adds `--voice` to `run` and `doctor` and calls `apply_voice_flags` instead of setting
  `model_path` itself.
- [ ] **Step 4: Run, expect PASS**; run the whole unit suite, expect no regressions.
- [ ] **Step 5: Commit** — `feat(voice): name a voice as <engine>:<voice> from flag, env or config`

### Task 2: Engine registry, `close()`, closing in the pipeline

**Files:**
- Create: `src/codecast/voice/registry.py`
- Modify: `src/codecast/voice/engines.py`, `src/codecast/preflight.py`, `src/codecast/pipeline.py:107-110,227`,
  `src/codecast/estimate.py:36`
- Test: `tests/unit/test_engines.py`, `tests/unit/test_preflight.py`, `tests/unit/test_estimate.py`

**Interfaces:**
- Consumes: `resolved_voice` (Task 1).
- Produces: `TTSEngine.close() -> None` (built-ins no-op); `registry.EngineKind` protocol with `name: str`,
  `status(voice_part: str, cfg: VoiceConfig, *, allow_network: bool) -> str`, `create(voice_part: str, cfg:
  VoiceConfig) -> TTSEngine`; `registry.register(kind: EngineKind) -> None`; `registry.kinds() -> dict[str,
  EngineKind]`; `select_engine(cfg, *, allow_network)` unchanged signature, now resolves named voices first;
  `engine_status(cfg, *, allow_network) -> dict[str, str]` gains `"model"`.

- [ ] **Step 1: Failing tests:** `test_named_piper_by_name_and_path`, `test_named_say_voice`,
  `test_named_edge_needs_network` (`edge-tts:x` without `allow_network` → `ConfigError` mentioning
  `--allow-network-tts`), `test_unknown_engine` (`ConfigError` listing `piper, say, edge-tts, model`),
  `test_named_voice_never_falls_back` (`piper:missing` with `say` available → `ConfigError`),
  `test_pipeline_closes_engine_it_created_not_given` (a fake engine counting `close()` calls: passed in → 0; created
  via a monkeypatched `select_engine` → 1, also when the run fails), `test_estimate_closes_its_engine`.
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement** the registry with piper, say and edge-tts kinds wrapping today's classes and status
  logic (probe order unchanged when `resolved_voice` is `None`). `pipeline.run` remembers whether it created the
  engine and closes it in the `finally`; `estimate_length` the same. Preflight's `tts` check reports
  `engine: <name> (<voice_id or voice part>)` for a named voice and is fatal on its status.
- [ ] **Step 4: Run, expect PASS**, full unit suite green.
- [ ] **Step 5: Commit** — `feat(voice): engine registry; engines close; the pipeline closes what it creates`

### Task 3: Voice file and `voice_id`

**Files:**
- Create: `src/codecast/voice/voicefile.py`
- Test: `tests/unit/test_voicefile.py`

**Interfaces:**
- Produces: `VoiceFile` (pydantic, `extra="forbid"`): `worker: str | None`, `command: list[str] | None`, `model: str |
  None`, `language: str | None`, `device: str = "auto"`, `reference: str | None`, `reference_text: str | None`,
  `options: dict = {}`, `platforms: dict[str, dict] = {}`; `load_voice_file(path: Path, *, platform: str =
  sys.platform) -> ResolvedVoice`; `ResolvedVoice` (frozen dataclass): `path: Path`, `worker: str | None`,
  `command: list[str] | None` (with `{voice_dir}` substituted), `model`, `language`, `device`, `reference: Path |
  None` (absolute), `reference_text`, `options: dict`, `voice_id: str`, `to_hello() -> dict`;
  `sentence_seed(text: str) -> int` (first 8 bytes of SHA-256 over NFC text, big-endian unsigned);
  `WORKERS_DIR: Path` (the packaged `voice/workers/` folder).

- [ ] **Step 1: Failing tests:** `test_unknown_key_rejected`, `test_worker_xor_command` (both or neither →
  `ConfigError`), `test_platform_override_applies` (`platforms.darwin.model` used only with `platform="darwin"`),
  `test_voice_id_stable_across_move` (copy file and clip to another folder → same id), `test_voice_id_changes_with`
  parametrised over: clip bytes, `model`, `language`, `device`, an `options` value, `reference_text`, a platform
  override, the shipped worker's `.py` bytes (monkeypatch `WORKERS_DIR` to a tmp copy) → id differs,
  `test_voice_id_ignores_options_order`, `test_voice_id_nfc` (NFC and NFD `reference_text` → same id),
  `test_voice_id_format` (`re.fullmatch(r"hi-narrator-[0-9a-f]{12}", id)`), `test_command_keyed_on_argv`,
  `test_missing_reference_is_config_error`, `test_sentence_seed_deterministic_and_nfc`.
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement** per spec 5.3 (hash input: canonical JSON with `sort_keys=True` of the resolved fields,
  then the clip bytes, then for a shipped worker `<name>.py` and `<name>.requirements.txt` bytes).
- [ ] **Step 4: Run, expect PASS.**
- [ ] **Step 5: Commit** — `feat(voice): voice files and voice_id`

### Task 4: The worker side of the protocol, and a fake worker

**Files:**
- Create: `src/codecast/voice/workers/__init__.py` (empty), `src/codecast/voice/workers/_protocol.py`,
  `tests/fake_worker.py`
- Modify: `pyproject.toml` package-data (`voice/workers/*.py`, `voice/workers/*.txt`)
- Test: `tests/unit/test_worker_protocol.py`

**Interfaces:**
- Produces: `_protocol.serve(handler) -> None`, where `handler` has `hello(voice: dict) -> dict` (returns the
  `ready` object), `synthesize(text: str, seed: int, out: str) -> None`, `design(text, language, instruct, seed, out)
  -> None` (may raise `NotImplementedError` → `ok:false`), and `_protocol.progress(msg: str) -> None`.
  `_protocol.setup_streams()` must run before any model import: keeps `os.dup(1)` as the protocol stream, then
  `os.dup2(2, 1)`; starts a daemon thread that blocks on stdin and calls `os._exit(0)` at EOF, feeding lines to the
  request loop via a queue. Replies: `{"id", "ok": true, ...}` / `{"id", "ok": false, "error"}`; unknown op or wrong
  `protocol` → `ok:false` (wrong protocol then exits).
- `tests/fake_worker.py` uses `_protocol` (imported by path) and writes a 0.5 s 24 kHz sine per sentence; behaviour
  switches come from the voice's `options`: `sleep_load`, `sleep_synth`, `crash_on` (sentence text), `stdout_noise`
  (prints to `sys.stdout` and `os.write(1, ...)`), `stderr_flood` (bytes), `ignore_quit`, `fail_design`, `spawn_child`
  (starts a sleeping grandchild and writes its pid to a file).

- [ ] **Step 1: Failing tests** (drive `fake_worker.py` with a raw `subprocess.Popen`, no client yet):
  `test_hello_synthesize_quit`, `test_stray_stdout_does_not_corrupt_protocol` (`stdout_noise`; every protocol line
  parses), `test_wrong_protocol_version_refused`, `test_unknown_op_is_ok_false`, `test_ascii_only_replies`.
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement** `_protocol.py` (stdlib only).
- [ ] **Step 4: Run, expect PASS.**
- [ ] **Step 5: Commit** — `feat(voice): worker protocol (worker side) and a fake worker for tests`

### Task 5: The worker client and the `model` engine

**Files:**
- Create: `src/codecast/voice/worker.py` (process management), `src/codecast/voice/model_engine.py`
- Modify: `src/codecast/voice/registry.py` (register `model`), `src/codecast/report.py` (`tts_worker: dict | None`),
  `src/codecast/pipeline.py:109-110` (record `tts_worker` after voicing), `pyproject.toml` (`gpu` marker)
- Test: `tests/unit/test_worker_client.py`, `tests/unit/test_model_engine.py`

**Interfaces:**
- Consumes: `ResolvedVoice`, `sentence_seed` (Task 3); `_protocol` and `fake_worker.py` (Task 4).
- Produces: `worker.Worker(argv: list[str], *, start_timeout_s: float, sentence_timeout_s: float, lock_device: str |
  None, lock_dir: Path, log: logging.Logger)` with `start(hello: dict) -> dict` (the `ready` object), `request(op:
  str, **fields) -> dict`, `close() -> None`, `stderr_tail() -> list[str]`; `worker.worker_env(environ:
  Mapping[str,str]) -> dict[str,str]` (the allowlist); `model_engine.ModelEngine(voice: ResolvedVoice, cfg:
  VoiceConfig)` with `name = "model"`, `voice_id`, `synthesize(text, out_wav)` (lazy start; sends `seed`;
  converts with `convert_to_wav48k`), `close()`, `ready: dict | None`; `model_engine.interpreter_for(worker: str,
  cfg: VoiceConfig) -> Path` (`worker_python[worker]` or `<envs_dir>/<worker>/Scripts/python.exe` / `bin/python`).
  The `model` kind's `status` checks spec 5.7's no-start list.

- [ ] **Step 1: Failing tests** (fake worker via `command:` voice files):
  `test_lazy_start` (create engine → no process; first `synthesize` → one), `test_one_process_across_calls`,
  `test_all_cached_run_starts_no_worker` (`synthesize_storyboard` over a pre-filled cache dir), `test_close_quits`,
  `test_close_kills_when_quit_ignored` (`ignore_quit`; returns within ~11 s, process gone),
  `test_worker_exits_on_stdin_eof_while_busy` (kill the client's pipes mid `sleep_synth`; worker gone within 5 s),
  `test_job_object_kills_children` (Windows only; `spawn_child`, close the job handle; grandchild gone) and
  `test_process_group_kills_children` (POSIX only), `test_sentence_timeout_fails_voice_stage` (`CodecastError`,
  `stage == "voice"`), `test_crash_reports_stderr_tail` (message contains the worker's last stderr line and the
  sentence), `test_stderr_flood_does_not_block` (`stderr_flood: 5_000_000` during load; `hello` completes),
  `test_hindi_text_and_spaced_path_round_trip` (cache dir `tmp_path / "a b/नमस्ते"`, Hindi sentence; WAV written),
  `test_env_allowlist` (`ANTHROPIC_API_KEY` set in CodeCast's env, absent in the worker's; `HF_HOME` present;
  `PYTHONUTF8 == "1"`), `test_gpu_lock_waits` (second `Worker` on the same `lock_device` waits while the first
  holds it, then starts after `close`), `test_cuda_oom_hint` (worker error containing `CUDA out of memory` → message
  mentions another run may hold the GPU), `test_ready_recorded_in_report`.
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement** per spec 5.4: `Popen` with argv list, `stdin/stdout/stderr=PIPE`, binary pipes,
  `env=worker_env(os.environ)`; Windows: `CREATE_NO_WINDOW`, then a Job Object via ctypes
  (`CreateJobObjectW`, `SetInformationJobObject` with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`,
  `AssignProcessToJobObject`), handle closed in `close()`; POSIX: `start_new_session=True`, `os.killpg`. One thread
  drains stderr (lines to `log.debug`, a `deque(maxlen=40)`), one reads stdout into a `queue.Queue`, non-JSON lines
  logged; `progress` lines logged at info. Lock: `<lock_dir>/<device>.lock` created with `O_CREAT|O_EXCL`, pid
  inside; stale lock (pid not alive) removed; poll every 1 s while waiting. `atexit` closes live workers (a
  `weakref.WeakSet`). `device` `auto` resolves inside the worker; the client locks on the voice's `device` string
  unless it is `cpu`.
- [ ] **Step 4: Run, expect PASS**, full unit suite green.
- [ ] **Step 5: Commit** — `feat(voice): model engine with a long-lived worker process`

### Task 6: The audio cache across rounds

**Files:**
- Modify: `src/codecast/voice/timeline.py:111-130`
- Test: `tests/unit/test_timeline.py`, `tests/unit/test_estimate.py`

**Interfaces:**
- Produces: `synthesize_storyboard` unchanged signature; before voicing a key missing from `cache_dir`, it looks
  for `<key>.wav` in `cache_dir.parent/*/` and copies it (`shutil.copy2`) into `cache_dir`.

- [ ] **Step 1: Failing tests:** `test_round1_reuses_round0_sentence` (counting fake engine: round0 voices 3,
  round1 with one changed sentence voices 1), `test_estimate_reuses_round0` (`estimate_length` after round0 → 0
  calls, and with a `ModelEngine` no worker starts), `test_tmp_files_not_reused` (`*.tmp.wav` siblings ignored).
- [ ] **Step 2: Run, expect FAIL.**  **Step 3: Implement.**  **Step 4: Run, expect PASS.**
- [ ] **Step 5: Commit** — `feat(voice): reuse a run's earlier rounds' audio`

### Task 7: The shipped workers and `codecast voice-env`

**Files:**
- Create: `src/codecast/voice/workers/omnivoice_worker.py`, `omnivoice.requirements.txt`, `mlx_audio_worker.py`,
  `mlx-audio.requirements.txt` (worker name `mlx-audio`, file `mlx_audio_worker.py`; the name→file map lives in
  `voicefile.py`), `src/codecast/voice/envs.py`
- Modify: `src/codecast/cli.py` (`voice-env` subcommand)
- Test: `tests/unit/test_envs.py`, `tests/gpu/test_omnivoice.py` (marker `gpu`)

**Interfaces:**
- Produces: `envs.create_env(worker: str, cfg: VoiceConfig, *, torch: str | None, runner=subprocess.run) -> Path`
  (returns the interpreter); `codecast voice-env <worker> [--torch cu128|cpu]`.
- Workers: `omnivoice_worker.py` loads `OmniVoice.from_pretrained(model, device_map=<device>, dtype=float16 on cuda else
  float32)`, prepares `create_voice_clone_prompt(ref_audio, ref_text)` in `hello`, seeds `torch.manual_seed(seed)`
  (and numpy/random) per request, generates with `options` passed as keyword arguments, writes with `soundfile` at
  24000; `design` uses `instruct`. `mlx_audio_worker.py` the same through mlx-audio's TTS API (`mx.random.seed`).
  Requirements pinned: `omnivoice==0.2.1`, `soundfile`, torch pinned by `envs.py` (`torch==2.8.0`,
  `torchaudio==2.8.0`, index `https://download.pytorch.org/whl/cu128` for `cu128`, `.../whl/cpu` for `cpu`, PyPI on
  the Mac).

- [ ] **Step 1: Failing tests:** `test_create_env_commands` (fake `runner` records argv: `uv venv --python 3.12
  <envs>/omnivoice`, then `uv pip install --python <interp> -r <req>` plus torch with `--index-url` for `cu128`),
  `test_default_torch_cu128_when_nvidia_smi` / `..._cpu_without`, `test_uv_missing_is_config_error`,
  `test_env_lock_refuses_second_builder`, `test_unknown_worker`. GPU test (`-m gpu`, run by hand):
  `test_omnivoice_voices_and_designs` (design a clip, voice a Hindi sentence from it twice with the same seed →
  identical samples; peak below 6 GiB).
- [ ] **Step 2: Run, expect FAIL.**  **Step 3: Implement.**  **Step 4: Run unit tests, expect PASS**; run
  `codecast voice-env omnivoice` and `pytest -m gpu tests/gpu -q` on the Windows GPU, expect PASS.
- [ ] **Step 5: Commit** — `feat(voice): omnivoice and mlx-audio workers; codecast voice-env`

### Task 8: Doctor and `codecast voice-design`

**Files:**
- Create: `src/codecast/voice/design.py`
- Modify: `src/codecast/preflight.py`, `src/codecast/cli.py` (`doctor --deep`, `voice-design`), `README.md`
  (a "Voices" section: naming, voice files, `voice-env`, `voice-design`, Performance mode on laptops)
- Test: `tests/unit/test_design.py`, `tests/unit/test_preflight.py`

**Interfaces:**
- Produces: `design.design_voice(out: Path, *, worker: str, model: str, language: str, instruct: str, text: str,
  options: dict, seed: int, cfg: VoiceConfig, force: bool = False) -> ResolvedVoice`; `run_checks(..., deep: bool =
  False)`.

- [ ] **Step 1: Failing tests:** `test_design_writes_clip_and_voice_file` (fake worker; `<stem>.wav` and the YAML
  with `reference`/`reference_text`; `load_voice_file` of it succeeds), `test_design_refuses_overwrite_without_force`
  (for the YAML and for the clip), `test_doctor_prints_command_of_voice`, `test_doctor_deep_reports_ready`,
  `test_doctor_warns_developer_mode_off` (Windows, monkeypatched registry read → WARN, not FAIL),
  `test_doctor_warns_voice_without_reference`.
- [ ] **Step 2: Run, expect FAIL.**  **Step 3: Implement** (Developer Mode: `HKLM\SOFTWARE\Microsoft\Windows\
  CurrentVersion\AppModelUnlock` `AllowDevelopmentWithoutDevLicense` via `winreg`).  **Step 4: Run, expect PASS.**
- [ ] **Step 5: Commit** — `feat(voice): doctor --deep and codecast voice-design`

### Task 9: End to end on the GPU, and the version

**Files:**
- Modify: `pyproject.toml` (version `1.2.0`, following the v1.x tags)
- Test: by hand

- [ ] **Step 1:** `codecast voice-design ~/voices/hi-test.yaml --worker omnivoice --model k2-fsa/OmniVoice
  --language hi --instruct "female, low pitch, indian accent" --text "<Hindi sentence>" --option num_step=16`;
  expect a clip and a `voice_id`.
- [ ] **Step 2:** `codecast run --repo <small repo> --voice model:~/voices/hi-test.yaml --from-stage voice --run
  <existing run>` (or `--compute-video-length`); expect the voice stage to start one worker, the report's
  `tts_worker` filled, and `nvidia-smi` showing no worker after it exits.
- [ ] **Step 3:** Full suite `pytest -q`, lint as the repo does; commit `chore: 1.2.0` and push.
