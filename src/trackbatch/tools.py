"""Locate Fiji / MATLAB / u-track and remember their paths between sessions."""
from __future__ import annotations

import glob
import json
import os
import platform
import shutil
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("TRACKBATCH_CONFIG_DIR", Path.home() / ".trackbatch"))
CONFIG_FILE = CONFIG_DIR / "settings.json"

# Launcher names, newest (Jaunch-based Fiji) first.
_FIJI_LAUNCHERS = {
    "Windows": ["fiji-windows-x64.exe", "ImageJ-win64.exe", "fiji-windows-arm64.exe", "ImageJ-win32.exe"],
    "Linux": ["fiji-linux-x64", "ImageJ-linux64", "fiji-linux-arm64"],
    "Darwin": ["Contents/MacOS/fiji-macos", "Contents/MacOS/ImageJ-macosx", "Contents/MacOS/fiji-macos-arm64"],
}


def load_app_settings() -> dict:
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_app_settings(settings: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(settings, indent=2), encoding="utf-8")


def _launcher_in(app_dir: Path) -> Path | None:
    for name in _FIJI_LAUNCHERS.get(platform.system(), []):
        cand = app_dir / name
        if cand.is_file():
            return cand
    return None


def resolve_fiji(path: str | None) -> Path | None:
    """Accept either a Fiji launcher or a Fiji.app folder; return the launcher."""
    candidates: list[Path] = []
    if path:
        candidates.append(Path(path))
    else:
        home = Path.home()
        candidates += [
            home / "Fiji.app", home / "Fiji", home / "Desktop" / "Fiji.app", home / "Applications" / "Fiji.app",
            Path("/Applications/Fiji.app"), Path("C:/Fiji.app"), Path("C:/Program Files/Fiji.app"),
            Path(os.environ.get("LOCALAPPDATA", home)) / "Fiji.app", Path("/opt/Fiji.app"),
        ]
    for cand in candidates:
        if cand.is_file():
            return cand
        if cand.is_dir():
            found = _launcher_in(cand)
            if found:
                return found
    return None


def resolve_matlab(path: str | None) -> Path | None:
    """Accept the matlab executable or a MATLAB install folder."""
    exe = "matlab.exe" if platform.system() == "Windows" else "matlab"
    if path:
        p = Path(path)
        if p.is_file():
            return p
        if p.is_dir():
            for cand in (p / "bin" / exe, p / exe):
                if cand.is_file():
                    return cand
        return None
    which = shutil.which("matlab")
    if which:
        return Path(which)
    patterns = {
        "Windows": ["C:/Program Files/MATLAB/R*/bin/matlab.exe"],
        "Darwin": ["/Applications/MATLAB_R*.app/bin/matlab"],
        "Linux": ["/usr/local/MATLAB/R*/bin/matlab", "/opt/MATLAB/R*/bin/matlab"],
    }.get(platform.system(), [])
    hits = sorted(h for pat in patterns for h in glob.glob(pat))
    return Path(hits[-1]) if hits else None  # newest release sorts last
