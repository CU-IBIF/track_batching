"""Application logging: one log file per session plus optional in-app listeners.

The windowed .exe has no console, so anything printed or raised in a Tk
callback would otherwise vanish. Everything goes to
``~/.trackbatch/logs/trackbatch_<timestamp>.log`` instead.
"""
from __future__ import annotations

import datetime as _dt
import logging
import platform
import sys
from pathlib import Path

from . import __version__
from . import tools

logger = logging.getLogger("trackbatch")
KEEP_LOGS = 30
_log_file: Path | None = None


def log_dir() -> Path:
    return tools.CONFIG_DIR / "logs"


def current_log_file() -> Path | None:
    return _log_file


def setup_logging(console: bool = False) -> Path | None:
    """Start the session log file (idempotent). Returns its path, or None if it can't be written."""
    global _log_file
    if _log_file is not None:
        return _log_file
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(threadName)s: %(message)s")
    try:
        d = log_dir()
        d.mkdir(parents=True, exist_ok=True)
        old = sorted(d.glob("trackbatch_*.log"))
        for f in old[: max(0, len(old) - KEEP_LOGS + 1)]:
            try:
                f.unlink()
            except OSError:
                pass
        path = d / f"trackbatch_{_dt.datetime.now():%Y%m%d_%H%M%S}.log"
        fh = logging.FileHandler(path, encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)
        _log_file = path
    except OSError:
        _log_file = None
    if console and sys.stderr is not None:
        sh = logging.StreamHandler(sys.stderr)
        sh.setLevel(logging.INFO)
        sh.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
        logger.addHandler(sh)
    logger.info("TrackBatch %s | Python %s | %s %s | frozen=%s",
                __version__, platform.python_version(), platform.system(), platform.release(),
                getattr(sys, "frozen", False))
    logger.info("Settings file: %s", tools.CONFIG_FILE)
    if _log_file:
        logger.info("Log file: %s", _log_file)
    return _log_file


class CallbackHandler(logging.Handler):
    """Forward log records to a callable (used by the GUI's activity panel)."""

    def __init__(self, callback, level=logging.DEBUG):
        super().__init__(level)
        self.callback = callback
        self.setFormatter(logging.Formatter("%(message)s"))

    def emit(self, record):
        try:
            self.callback(record.levelno, _dt.datetime.fromtimestamp(record.created), self.format(record))
        except Exception:  # never let the log break the app
            pass
