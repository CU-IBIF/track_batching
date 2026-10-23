"""Build and run batches of TrackMate / u-track jobs.

Each worker is one external process (Fiji headless or MATLAB -batch) that
processes a JSON manifest of jobs, so the slow start-up is paid once per worker
rather than once per image. Workers write ``trackbatch_status.json`` into every
job folder; that file is the source of truth for success/failure.
"""
from __future__ import annotations

import csv
import datetime as _dt
import json
import math
import os
import platform
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from importlib import resources
from pathlib import Path
from typing import Callable

from . import __version__
from . import params as P
from . import tools

IMAGE_EXTENSIONS = (".tif", ".tiff", ".ome.tif", ".nd2", ".czi", ".lif", ".lsm", ".ims", ".oib", ".oir", ".vsi", ".stk")
STATUS_FILE = "trackbatch_status.json"
RUN_FILE = "trackbatch_run.json"
WORK_DIR = "_trackbatch"

LogFn = Callable[[str], None]
ProgressFn = Callable[["Job"], None]


@dataclass
class Job:
    id: int
    image: str
    output_dir: str
    status: str = "pending"  # pending | running | ok | failed | skipped
    error: str = ""
    stats: dict = field(default_factory=dict)


@dataclass
class BatchConfig:
    engine: str
    params: dict
    inputs: list[str]
    output_dir: str
    fiji_path: str | None = None
    matlab_path: str | None = None
    utrack_path: str | None = None
    workers: int = 1
    dry_run: bool = False
    skip_existing: bool = False
    source: str | None = None  # previous run the params came from, for the record


def discover_inputs(paths: list[str], pattern: str | None = None, recursive: bool = False) -> list[Path]:
    """Expand files/folders into a sorted, de-duplicated list of image files."""
    out: list[Path] = []
    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            globber = p.rglob if recursive else p.glob
            if pattern:
                hits = globber(pattern)
            else:
                hits = (h for h in globber("*") if h.name.lower().endswith(IMAGE_EXTENSIONS))
            out.extend(h for h in hits if h.is_file())
        elif p.is_file():
            out.append(p)
        else:
            raise FileNotFoundError(f"Input not found: {raw}")
    seen, unique = set(), []
    for p in sorted(out):
        key = str(p.resolve())
        if key not in seen:
            seen.add(key)
            unique.append(p)
    return unique


def _stem(path: Path) -> str:
    name = path.name
    for ext in sorted(IMAGE_EXTENSIONS, key=len, reverse=True):
        if name.lower().endswith(ext):
            return name[: -len(ext)]
    return path.stem


def build_jobs(cfg: BatchConfig) -> list[Job]:
    out_root = Path(cfg.output_dir)
    used: dict[str, int] = {}
    jobs = []
    for i, img in enumerate(cfg.inputs):
        stem = _stem(Path(img))
        n = used.get(stem, 0)
        used[stem] = n + 1
        folder = stem if n == 0 else f"{stem}_{n + 1}"
        jobs.append(Job(i, str(Path(img).resolve()), str((out_root / folder).resolve())))
    return jobs


def _json_safe(obj):
    """MATLAB's jsondecode rejects Infinity/NaN; replace them with large numbers / null."""
    if isinstance(obj, float):
        if math.isnan(obj):
            return None
        if math.isinf(obj):
            return 1e308 if obj > 0 else -1e308
        return obj
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_json_safe(v) for v in obj]
    return obj


def _resource_text(name: str) -> str:
    return resources.files("trackbatch").joinpath("resources", name).read_text(encoding="utf-8")


def _matlab_str(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


class _Engine:
    name = ""

    def install(self, work_dir: Path) -> None: ...

    def command(self, cfg: BatchConfig, work_dir: Path, manifest: Path) -> tuple[list[str], dict]: ...

    def check(self, cfg: BatchConfig) -> list[str]:
        return []


class TrackMateEngine(_Engine):
    name = "trackmate"
    script = "trackmate_batch.py"

    def install(self, work_dir: Path) -> None:
        (work_dir / self.script).write_text(_resource_text("trackmate_batch.jy"), encoding="utf-8")

    def check(self, cfg: BatchConfig) -> list[str]:
        if tools.resolve_fiji(cfg.fiji_path) is None:
            return ["Fiji not found - set the path to Fiji.app (or its ImageJ/fiji launcher)."]
        return []

    def command(self, cfg, work_dir, manifest):
        fiji = tools.resolve_fiji(cfg.fiji_path) or Path(cfg.fiji_path or "ImageJ")
        cmd = [str(fiji), "--headless", "--console", "--run", str(work_dir / self.script)]
        return cmd, {"TRACKBATCH_MANIFEST": str(manifest)}


class UTrackEngine(_Engine):
    name = "utrack"

    def install(self, work_dir: Path) -> None:
        for name in ("trackbatch_utrack_batch.m", "trackbatch_export_params.m"):
            (work_dir / name).write_text(_resource_text(name), encoding="utf-8")

    def check(self, cfg: BatchConfig) -> list[str]:
        problems = []
        if tools.resolve_matlab(cfg.matlab_path) is None:
            problems.append("MATLAB not found - set the path to the matlab executable.")
        if cfg.utrack_path and not Path(cfg.utrack_path).is_dir():
            problems.append(f"u-track folder does not exist: {cfg.utrack_path}")
        return problems

    def command(self, cfg, work_dir, manifest):
        matlab = tools.resolve_matlab(cfg.matlab_path) or Path(cfg.matlab_path or "matlab")
        stmt = f"addpath({_matlab_str(str(work_dir))}); trackbatch_utrack_batch({_matlab_str(str(manifest))});"
        return [str(matlab), "-batch", stmt], {}


ENGINES: dict[str, _Engine] = {"trackmate": TrackMateEngine(), "utrack": UTrackEngine()}


def _write_run_file(cfg: BatchConfig, jobs: list[Job], started: str, finished: str | None) -> Path:
    out = Path(cfg.output_dir) / RUN_FILE
    record = {
        "trackbatch_version": __version__,
        "engine": cfg.engine,
        "params": cfg.params,
        "inputs": list(cfg.inputs),
        "output_dir": str(Path(cfg.output_dir).resolve()),
        "params_source": cfg.source,
        "started": started,
        "finished": finished,
        "jobs": [asdict(j) for j in jobs],
    }
    out.write_text(P.dumps(record), encoding="utf-8")
    return out


def _write_summary(cfg: BatchConfig, jobs: list[Job]) -> Path:
    path = Path(cfg.output_dir) / "batch_summary.csv"
    stat_keys = sorted({k for j in jobs for k in j.stats})
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["image", "output_dir", "status", *stat_keys, "error"])
        for j in jobs:
            w.writerow([j.image, j.output_dir, j.status, *[j.stats.get(k, "") for k in stat_keys], j.error])
    return path


def _read_status(job: Job) -> None:
    status_path = Path(job.output_dir) / STATUS_FILE
    try:
        data = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    job.status = data.get("status", job.status)
    job.error = data.get("error", "") or ""
    job.stats = data.get("stats", {}) or {}


def run_batch(
    cfg: BatchConfig,
    log: LogFn = print,
    progress: ProgressFn | None = None,
    stop_event: threading.Event | None = None,
) -> list[Job]:
    if cfg.engine not in ENGINES:
        raise ValueError(f"Unknown engine {cfg.engine!r}")
    if not cfg.inputs:
        raise ValueError("No input images")
    stop_event = stop_event or threading.Event()

    out_root = Path(cfg.output_dir).resolve()
    work_dir = out_root / WORK_DIR
    work_dir.mkdir(parents=True, exist_ok=True)
    # Everything shown to the user also goes to <output>/_trackbatch/batch.log.
    log_lock = threading.Lock()
    log_fh = open(work_dir / "batch.log", "a", encoding="utf-8")
    user_log = log

    def log(msg: str) -> None:
        with log_lock:
            log_fh.write(f"{_dt.datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n")
            log_fh.flush()
        user_log(msg)

    try:
        return _run_batch(cfg, log, progress, stop_event, work_dir)
    except Exception as exc:
        log(f"ERROR: batch aborted: {type(exc).__name__}: {exc}")
        raise
    finally:
        log_fh.close()


def _run_batch(cfg: BatchConfig, log: LogFn, progress: ProgressFn | None, stop_event: threading.Event,
               work_dir: Path) -> list[Job]:
    engine = ENGINES[cfg.engine]
    log(f"=== trackbatch {__version__}: {cfg.engine}, {len(cfg.inputs)} image(s), {cfg.workers} worker(s)"
        f"{', dry run' if cfg.dry_run else ''}, output {Path(cfg.output_dir).resolve()}")
    engine.install(work_dir)
    log(f"Worker scripts written to {work_dir}")

    jobs = build_jobs(cfg)
    todo: list[Job] = []
    for job in jobs:
        Path(job.output_dir).mkdir(parents=True, exist_ok=True)
        if cfg.skip_existing:
            _read_status(job)
            if job.status == "ok":
                job.status = "skipped"
                log(f"Skipping {Path(job.image).name} (already done)")
                if progress:
                    progress(job)
                continue
            job.status, job.error, job.stats = "pending", "", {}
        # A per-image copy of the parameters so any single result can seed a new run.
        P.save_params(Path(job.output_dir) / "trackbatch_params.json", cfg.engine, cfg.params)
        stale = Path(job.output_dir) / STATUS_FILE
        if stale.exists():
            stale.unlink()
        todo.append(job)

    started = _dt.datetime.now().isoformat(timespec="seconds")
    _write_run_file(cfg, jobs, started, None)

    if not cfg.dry_run:
        problems = engine.check(cfg)
        if problems:
            raise RuntimeError("\n".join(problems))
        tool = (tools.resolve_fiji(cfg.fiji_path) if cfg.engine == "trackmate"
                else tools.resolve_matlab(cfg.matlab_path))
        log(f"Using {tool}")
    if not todo:
        log("Nothing to do - every image was skipped")

    n_workers = max(1, min(cfg.workers, len(todo))) if todo else 0
    chunks = [todo[i::n_workers] for i in range(n_workers)]
    by_id = {j.id: j for j in jobs}
    lock = threading.Lock()

    def run_chunk(k: int, chunk: list[Job]) -> None:
        manifest = work_dir / f"manifest_{k}.json"
        payload = {
            "engine": cfg.engine,
            "params": cfg.params,
            "utrack_path": cfg.utrack_path,
            "jobs": [{"id": j.id, "image": j.image, "output_dir": j.output_dir} for j in chunk],
        }
        if cfg.engine == "utrack":
            payload = _json_safe(payload)
        manifest.write_text(P.dumps(payload), encoding="utf-8")
        cmd, extra_env = engine.command(cfg, work_dir, manifest)
        if cfg.dry_run:
            env_txt = " ".join(f"{k}={v}" for k, v in extra_env.items())
            log(f"[dry-run] worker {k}: {env_txt + ' ' if env_txt else ''}{subprocess.list2cmdline(cmd)}")
            return
        if stop_event.is_set():
            return
        env = dict(os.environ, **extra_env)
        kwargs = {}
        if platform.system() == "Windows":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        log(f"Worker {k}: starting for {len(chunk)} image(s): {subprocess.list2cmdline(cmd)}")
        for key, value in extra_env.items():
            log(f"Worker {k}: {key}={value}")
        t_start = time.monotonic()
        markers = 0
        try:
            proc = subprocess.Popen(
                cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                text=True, encoding="utf-8", errors="replace", bufsize=1, **kwargs,
            )
        except OSError as exc:
            log(f"Worker {k}: could not start {cmd[0]}: {exc}")
            for job in chunk:
                job.status, job.error = "failed", f"could not start {Path(cmd[0]).name}: {exc}"
                if progress:
                    progress(job)
            return
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip()
            if not line:
                continue
            log(f"[{k}] {line}")
            # Progress markers printed by the worker scripts.
            if line.startswith("[trackbatch] JOB "):
                markers += 1
                parts = line.split()
                try:
                    job = by_id[int(parts[2])]
                except (IndexError, ValueError, KeyError):
                    continue
                with lock:
                    if parts[3] == "START":
                        job.status = "running"
                    else:
                        _read_status(job)
                if progress:
                    progress(job)
            if stop_event.is_set():
                proc.terminate()
                break
        code = proc.wait()
        log(f"Worker {k}: exited with code {code} after {time.monotonic() - t_start:.1f} s")
        if markers == 0 and not stop_event.is_set():
            log(f"Worker {k}: WARNING - no TrackBatch progress lines were printed, so the worker script "
                "probably never ran. Check the tool path and the output above; you can re-run the command "
                "shown at the start of this worker in a terminal to see the full error.")
        for job in chunk:
            _read_status(job)
            if job.status in ("pending", "running"):
                job.status = "failed"
                job.error = "cancelled" if stop_event.is_set() else f"worker exited with code {code} before finishing"
                if progress:
                    progress(job)

    if chunks:
        with ThreadPoolExecutor(max_workers=n_workers) as pool:
            futures = [pool.submit(run_chunk, k, c) for k, c in enumerate(chunks)]
            for fut in futures:
                fut.result()

    finished = _dt.datetime.now().isoformat(timespec="seconds")
    if not cfg.dry_run:
        run_file = _write_run_file(cfg, jobs, started, finished)
        summary = _write_summary(cfg, jobs)
        n_ok = sum(j.status in ("ok", "skipped") for j in jobs)
        log(f"Finished: {n_ok}/{len(jobs)} succeeded. Summary: {summary}  Run file: {run_file}")
    else:
        log(f"[dry-run] Scripts and manifests written to {work_dir}")
    return jobs
