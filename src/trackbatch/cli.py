"""Command-line interface.

Examples::

    trackbatch run -e trackmate -i movies/ --pattern "*.tif" -o results/ --set detector.RADIUS=3
    trackbatch run -e utrack -i movies/ -o results/ --from-previous old/tracking.mat
    trackbatch check -e trackmate -i movies/ --set detector.TARGET_CHANNEL=2
    trackbatch params -e trackmate --from-previous old_run.xml -o params.json
    trackbatch gui
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from . import params as P
from . import tools
from .batch import BatchConfig, discover_inputs, run_batch
from .precheck import precheck
from .previous import load_previous, matlab_export_params


def _resolve_params(args) -> tuple[str, dict, str | None, list[str]]:
    """Combine defaults, --from-previous, --params and --set (in that order)."""
    engine = args.engine
    source = None
    prev_inputs: list[str] = []
    if args.from_previous:
        prev = load_previous(args.from_previous, engine)
        if engine and prev.engine != engine:
            raise SystemExit(f"{args.from_previous} holds {prev.engine} parameters but --engine is {engine}")
        engine, params, source, prev_inputs = prev.engine, prev.params, str(args.from_previous), prev.inputs
        for note in prev.notes:
            print(note, file=sys.stderr)
    else:
        if not engine:
            raise SystemExit("--engine is required unless --from-previous is given")
        params = P.defaults(engine)
    if getattr(args, "params", None):
        data = json.loads(Path(args.params).read_text(encoding="utf-8"))
        params = P.deep_merge(params, data.get("params", data))
        source = source or str(args.params)
    if getattr(args, "set", None):
        params = P.apply_overrides(params, args.set)
    return engine, params, source, prev_inputs


def cmd_run(args) -> int:
    engine, params, source, prev_inputs = _resolve_params(args)
    raw_inputs = list(args.input or [])
    if args.reuse_inputs:
        raw_inputs += prev_inputs
    if not raw_inputs:
        raise SystemExit("No inputs: use -i/--input (files or folders) or --reuse-inputs")
    inputs = [str(p) for p in discover_inputs(raw_inputs, args.pattern, args.recursive)]
    if not inputs:
        raise SystemExit("No images matched the given inputs/pattern")
    saved = tools.load_app_settings()
    cfg = BatchConfig(
        engine=engine,
        params=params,
        inputs=inputs,
        output_dir=args.output,
        fiji_path=args.fiji or saved.get("fiji_path"),
        matlab_path=args.matlab or saved.get("matlab_path"),
        utrack_path=args.utrack or saved.get("utrack_path"),
        workers=args.workers,
        dry_run=args.dry_run,
        skip_existing=args.skip_existing,
        source=source,
    )
    print(f"{engine}: {len(inputs)} image(s) -> {Path(args.output).resolve()}")
    if not args.no_precheck:
        result = precheck(engine, params, inputs)
        report = result.report()
        print(report)
        Path(args.output).mkdir(parents=True, exist_ok=True)
        (Path(args.output) / "precheck.txt").write_text(report + "\n", encoding="utf-8")
        if result.errors and not args.dry_run:
            print(f"error: pre-check found {len(result.errors)} problem(s); fix them, remove those images, "
                  "or pass --no-precheck to run anyway", file=sys.stderr)
            return 2
    try:
        jobs = run_batch(cfg)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0 if all(j.status in ("ok", "skipped", "pending") for j in jobs) else 1


def cmd_check(args) -> int:
    engine, params, _, prev_inputs = _resolve_params(args)
    raw_inputs = list(args.input or []) + (prev_inputs if args.reuse_inputs else [])
    if not raw_inputs:
        raise SystemExit("No inputs: use -i/--input (files or folders) or --reuse-inputs")
    inputs = [str(p) for p in discover_inputs(raw_inputs, args.pattern, args.recursive)]
    result = precheck(engine, params, inputs)
    print(result.report())
    return 1 if result.errors else 0


def cmd_params(args) -> int:
    if args.matlab_export:
        saved = tools.load_app_settings()
        prev = matlab_export_params(
            args.matlab_export, args.matlab or saved.get("matlab_path"), args.utrack or saved.get("utrack_path")
        )
        engine, params = prev.engine, prev.params
        for note in prev.notes:
            print(note, file=sys.stderr)
    else:
        engine, params, _, _ = _resolve_params(args)
    text = P.dumps({"engine": engine, "params": params})
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
        print(f"Wrote {args.output}")
    else:
        print(text)
    return 0


def cmd_config(args) -> int:
    saved = tools.load_app_settings()
    for key in ("fiji", "matlab", "utrack"):
        value = getattr(args, key)
        if value is not None:
            saved[f"{key}_path"] = value
    if any(getattr(args, k) is not None for k in ("fiji", "matlab", "utrack")):
        tools.save_app_settings(saved)
    print(f"Settings file: {tools.CONFIG_FILE}")
    print(f"  fiji_path:   {saved.get('fiji_path')}  -> {tools.resolve_fiji(saved.get('fiji_path'))}")
    print(f"  matlab_path: {saved.get('matlab_path')}  -> {tools.resolve_matlab(saved.get('matlab_path'))}")
    print(f"  utrack_path: {saved.get('utrack_path')}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="trackbatch", description="Batch TrackMate / u-track analyses.")
    ap.add_argument("--version", action="version", version=f"trackbatch {__version__}")
    sub = ap.add_subparsers(dest="command")

    def add_param_args(p):
        p.add_argument("-e", "--engine", choices=P.ENGINES, help="analysis engine")
        p.add_argument("--from-previous", metavar="FILE",
                       help="reuse parameters of a previous run (.xml TrackMate, .mat u-track, trackbatch .json)")
        p.add_argument("--params", metavar="JSON", help="parameter file (applied after --from-previous)")
        p.add_argument("--set", action="append", metavar="KEY=VALUE",
                       help="override one parameter, e.g. detector.RADIUS=3 or "
                            "tracking.gapCloseParam.timeWindow=7 (repeatable)")

    def add_input_args(p):
        p.add_argument("-i", "--input", action="append", help="image file or folder (repeatable)")
        p.add_argument("--pattern", help="glob for folders, e.g. '*.tif' (default: common image types)")
        p.add_argument("-r", "--recursive", action="store_true", help="search folders recursively")
        p.add_argument("--reuse-inputs", action="store_true", help="also process the images of --from-previous")

    run = sub.add_parser("run", help="run a batch")
    add_param_args(run)
    add_input_args(run)
    run.add_argument("--no-precheck", action="store_true",
                     help="don't inspect the images first (errors found by the pre-check normally stop the run)")
    run.add_argument("-o", "--output", required=True, help="output folder")
    run.add_argument("-w", "--workers", type=int, default=1, help="parallel Fiji/MATLAB processes")
    run.add_argument("--skip-existing", action="store_true", help="skip images that already succeeded")
    run.add_argument("--dry-run", action="store_true", help="write scripts/manifests, print commands, don't run")
    run.add_argument("--fiji", help="Fiji.app folder or launcher")
    run.add_argument("--matlab", help="matlab executable")
    run.add_argument("--utrack", help="u-track 'software' folder (added to the MATLAB path)")
    run.set_defaults(func=cmd_run)

    chk = sub.add_parser("check", help="inspect images (size, channels, frames, calibration, bit depth) "
                                       "against the parameters without running anything")
    add_param_args(chk)
    add_input_args(chk)
    chk.set_defaults(func=cmd_check)

    prm = sub.add_parser("params", help="print/save a parameter set (defaults or from a previous run)")
    add_param_args(prm)
    prm.add_argument("--matlab-export", metavar="MAT",
                     help="read a u-track movieData.mat / v7.3 .mat via MATLAB")
    prm.add_argument("--matlab", help="matlab executable")
    prm.add_argument("--utrack", help="u-track 'software' folder")
    prm.add_argument("-o", "--output", help="write to this JSON file instead of stdout")
    prm.set_defaults(func=cmd_params)

    cfg = sub.add_parser("config", help="show or store tool locations")
    cfg.add_argument("--fiji")
    cfg.add_argument("--matlab")
    cfg.add_argument("--utrack")
    cfg.set_defaults(func=cmd_config)

    gui = sub.add_parser("gui", help="open the graphical interface")
    gui.set_defaults(func=lambda a: _gui())

    st = sub.add_parser("selftest", help=argparse.SUPPRESS)  # used by CI on the built exe
    st.add_argument("--report", required=True)
    st.set_defaults(func=lambda a: _selftest(a.report))
    return ap


def _selftest(report: str) -> int:
    from .gui import selftest

    return selftest(report)


def _gui() -> int:
    from .gui import main as gui_main

    gui_main()
    return 0


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        # Windows consoles may not be UTF-8 (e.g. "µm" in reports); never crash on printing.
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(errors="replace")
            except (ValueError, OSError):
                pass
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        return _gui()
    try:
        return args.func(args)
    except (ValueError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
