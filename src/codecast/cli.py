"""codecast run | doctor | fetch-voice | eval (spec §10.1)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from codecast.config import apply_voice_flags, load_config
from codecast.errors import CodecastError, ConfigError
from codecast.pipeline import RunOptions
from codecast.pipeline import run as pipeline_run
from codecast.preflight import run_checks
from codecast.settings import load_env_file


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="codecast", description="GitHub repo → grounded explainer video")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="generate a video")
    r.add_argument("--repo", help="public https GitHub URL, local git repo, or plain local folder "
                   "(required unless --compute-video-length)")
    r.add_argument("--out", default="output", type=Path)
    r.add_argument("--config", type=Path)
    r.add_argument("--max-steps", type=int)
    r.add_argument("--allow-network-tts", action="store_true")
    r.add_argument("--from-stage", choices=["voice", "render", "compose"])
    r.add_argument("--run", dest="run_id")
    r.add_argument("--workspace", type=Path, default=Path("workspace"))
    r.add_argument("--compute-video-length", action="store_true",
                   help="speak the storyboard of --run <id> and print the video length; no LLM, no tokens, no video")
    r.add_argument("--verbose", action="store_true", help="print progress; run.log in the workspace always has it")
    r.add_argument("--voice", help="name the voice as <engine>:<voice> (piper, say, edge-tts, model); "
                   "default: CODECAST_VOICE, else voice.voice, else the first engine that works")
    r.add_argument("--voice-model", type=Path, help="custom piper voice .onnx (its .onnx.json must sit next to it); "
                   "default: CODECAST_VOICE_MODEL, else the configured piper voice")
    d = sub.add_parser("doctor", help="check the local setup")
    d.add_argument("--config", type=Path)
    d.add_argument("--voice-model", type=Path, help="check this custom piper voice")
    d.add_argument("--voice", help="check this voice, <engine>:<voice>")
    d.add_argument("--deep", action="store_true", help="also start the model voice's worker and send hello")
    f = sub.add_parser("fetch-voice", help="download the piper voice")
    f.add_argument("--config", type=Path)
    v = sub.add_parser("voice-env", help="build a shipped voice worker's environment (uv)")
    v.add_argument("worker", help="omnivoice or mlx-audio")
    v.add_argument("--torch", choices=["cu128", "cpu"], help="PyTorch build (default: cu128 with an NVIDIA GPU, else cpu)")
    v.add_argument("--config", type=Path)
    g = sub.add_parser("voice-design", help="design a new voice with a shipped worker; writes <out> and its clip")
    g.add_argument("out", type=Path, help="the new voice file (its clip is written beside it as <stem>.wav)")
    g.add_argument("--worker", required=True, help="omnivoice or mlx-audio")
    g.add_argument("--model", required=True)
    g.add_argument("--language", required=True)
    g.add_argument("--instruct", required=True, help="a description of the voice")
    g.add_argument("--text", required=True, help="a sentence or two the voice speaks; kept as reference_text")
    g.add_argument("--option", action="append", default=[], metavar="K=V", help="a worker option (V is a YAML scalar)")
    g.add_argument("--seed", type=int, help="default: derived from --text")
    g.add_argument("--force", action="store_true", help="overwrite an existing voice file and clip")
    g.add_argument("--config", type=Path)
    e = sub.add_parser("eval", help="evaluate a generated output directory")
    e.add_argument("out_dir", type=Path)
    e.add_argument("--no-reclone", action="store_true", help="skip the independent re-check against a fresh clone")
    e.add_argument("--source", help="clone the re-check from this path or URL instead of repo_url")
    return p


def _parse_options(items: list[str]) -> dict:
    import yaml
    options: dict = {}
    for item in items:
        key, sep, value = item.partition("=")
        if not sep or not key:
            raise ConfigError(f"--option {item!r}: expected K=V")
        try:
            options[key] = yaml.safe_load(value)
        except yaml.YAMLError as e:
            raise ConfigError(f"--option {item!r}: value is not valid YAML ({e})") from e
    return options


def _compute_video_length(args: argparse.Namespace, cfg) -> int:
    if not args.run_id:
        raise ConfigError("--compute-video-length needs --run <run_id>: it measures that run's storyboard.json")
    if args.from_stage:
        raise ConfigError("--compute-video-length cannot be combined with --from-stage")
    from codecast.estimate import estimate_length, format_estimate
    ws = args.workspace / args.run_id
    est = estimate_length(ws, cfg, allow_network=args.allow_network_tts)
    print(f"{format_estimate(est)}\nsaved: {ws / 'length_estimate.json'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        load_env_file(Path(".env"))
        cfg = load_config(getattr(args, "config", None))
        # sets the flag layer and checks the choice: a same-layer conflict (even both env variables) is exit 7
        apply_voice_flags(cfg, voice=getattr(args, "voice", None), voice_model=getattr(args, "voice_model", None))
        if args.cmd == "doctor":
            checks = run_checks(cfg, require_key=False, deep=args.deep)
            for c in checks:
                tag = "OK  " if c.ok else ("FAIL" if c.fatal else "WARN")
                print(f"[{tag}] {c.name}: {c.message}" + (f"\n       fix: {c.fix}" if c.fix and not c.ok else ""))
            return 7 if any(c.fatal and not c.ok for c in checks) else 0
        if args.cmd == "fetch-voice":
            from codecast.voice.fetch_voice import fetch_voice
            fetch_voice(cfg.voice)
            return 0
        if args.cmd == "voice-env":
            from codecast.voice.envs import create_env
            print(f"voice environment ready: {create_env(args.worker, cfg.voice, torch=args.torch)}")
            return 0
        if args.cmd == "voice-design":
            from codecast.voice.design import design_voice
            voice = design_voice(args.out, worker=args.worker, model=args.model, language=args.language,
                                 instruct=args.instruct, text=args.text, options=_parse_options(args.option),
                                 seed=args.seed, cfg=cfg.voice, force=args.force)
            print(f"voice_id: {voice.voice_id}\nvoice file: {voice.path}\nreference clip: {voice.reference}")
            return 0
        if args.cmd == "eval":
            from codecast.eval import evaluate
            return evaluate(args.out_dir, reclone=not args.no_reclone, source=args.source)
        if args.compute_video_length:
            return _compute_video_length(args, cfg)
        if not args.repo:
            raise ConfigError("--repo is required (or use --run <id> --compute-video-length to measure a run)")
        opts = RunOptions(repo=args.repo, out_dir=args.out, workspace=args.workspace, max_steps=args.max_steps,
                          allow_network_tts=args.allow_network_tts, from_stage=args.from_stage, run_id=args.run_id,
                          verbose=args.verbose)
        report = pipeline_run(opts, cfg)
        ws = args.workspace / report.run_id
        if report.exit_code == 0:
            print(f"codecast OK: {report.video.duration_s:.1f}s → {report.video.path}\n"
                  f"run report: {ws / 'run_report.json'}")
        else:
            print(f"codecast FAILED (exit {report.exit_code}) at stage {report.failure.stage}\n"
                  f"  {report.failure.message}\n  full log: {ws / 'run.log'}\n  run report: {ws / 'run_report.json'}",
                  file=sys.stderr)
        return report.exit_code
    except CodecastError as e:
        print(f"codecast: {e.message}", file=sys.stderr)
        return e.exit_code


def main_entry() -> None:
    sys.exit(main())
