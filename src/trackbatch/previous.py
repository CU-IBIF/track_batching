"""Load parameters from a previous TrackMate / u-track / trackbatch run.

Supported sources:

* ``*.json``  - a trackbatch run file (``trackbatch_run.json``) or params file.
* ``*.xml``   - a TrackMate file saved by Fiji or by trackbatch.
* ``*.mat``   - u-track output: detection results (``movieParam``/``detectionParam``),
  tracking results (``costMatrices``/``gapCloseParam``/``kalmanFunctions``),
  or a JSON dump of u-track 2.x ``movieData.mat`` process parameters produced by
  ``trackbatch params --matlab-export`` (needs MATLAB for classdef objects).
"""
from __future__ import annotations

import json
import math
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import params as P


@dataclass
class PreviousRun:
    engine: str
    params: dict
    inputs: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def load_previous(path: str | Path, engine_hint: str | None = None) -> PreviousRun:
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".json":
        return _load_json(path, engine_hint)
    if suffix == ".xml":
        return load_trackmate_xml(path)
    if suffix == ".mat":
        return load_utrack_mat(path)
    raise ValueError(f"Don't know how to read parameters from {path.name} (expected .json, .xml or .mat)")


# --------------------------------------------------------------------------
# trackbatch JSON
# --------------------------------------------------------------------------

def _load_json(path: Path, engine_hint: str | None) -> PreviousRun:
    data = json.loads(path.read_text(encoding="utf-8"))
    if "params" in data:
        engine = data.get("engine") or engine_hint
        params = data["params"]
        inputs = [j["image"] for j in data.get("jobs", []) if "image" in j] or list(data.get("inputs", []))
    else:
        # Bare params dict - or a MATLAB export of u-track structs.
        found = _search_utrack_structs(data)
        if found:
            return _utrack_from_found(found, [f"Read u-track structs from {path.name}"])
        engine = engine_hint or ("trackmate" if "detector" in data else "utrack")
        params, inputs = data, []
    if engine not in P.ENGINES:
        raise ValueError(f"{path.name}: cannot tell which engine these parameters are for")
    merged = P.deep_merge(P.defaults(engine), params)
    return PreviousRun(engine, merged, inputs, [f"Loaded trackbatch parameters from {path.name}"])


# --------------------------------------------------------------------------
# TrackMate XML
# --------------------------------------------------------------------------

def _xml_value(text: str) -> Any:
    low = text.strip().lower()
    if low == "true":
        return True
    if low == "false":
        return False
    if low in ("infinity", "+infinity"):
        return math.inf
    if low == "-infinity":
        return -math.inf
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return text


# Feature-penalty sub-elements of the LAP trackers -> their settings-map key.
_PENALTY_KEYS = {
    "Linking": "LINKING_FEATURE_PENALTIES",
    "GapClosing": "GAP_CLOSING_FEATURE_PENALTIES",
    "TrackSplitting": "SPLITTING_FEATURE_PENALTIES",
    "TrackMerging": "MERGING_FEATURE_PENALTIES",
}


def _flatten_settings(elem: ET.Element, out: dict, parent_tag: str | None = None) -> None:
    """TrackMate nests some tracker settings in sub-elements; the settings map is flat."""
    if elem.tag == "FeaturePenalties":
        key = _PENALTY_KEYS.get(parent_tag or "", f"{(parent_tag or '').upper()}_FEATURE_PENALTIES")
        penalties = {k: float(v) for k, v in elem.attrib.items()}
        # Older files used <FeaturePenalty feature=".." value=".."/> children.
        for child in elem:
            if "feature" in child.attrib and "value" in child.attrib:
                penalties[child.attrib["feature"]] = float(child.attrib["value"])
        out[key] = penalties
        return
    for k, v in elem.attrib.items():
        out[k] = _xml_value(v)
    for child in elem:
        _flatten_settings(child, out, elem.tag)


def _filters(elem: ET.Element | None) -> list[dict]:
    if elem is None:
        return []
    return [
        {
            "feature": f.attrib["feature"],
            "value": float(f.attrib["value"]),
            "is_above": f.attrib.get("isabove", "true").lower() == "true",
        }
        for f in elem.findall("Filter")
    ]


def load_trackmate_xml(path: str | Path) -> PreviousRun:
    path = Path(path)
    root = ET.parse(path).getroot()
    settings = root.find("Settings")
    if settings is None:
        raise ValueError(f"{path.name} has no <Settings> element - is it a TrackMate file?")

    params = P.trackmate_defaults()
    notes = [f"Loaded TrackMate settings from {path.name}"]

    det_el = settings.find("DetectorSettings")
    if det_el is not None:
        det: dict = {}
        _flatten_settings(det_el, det)
        name = det.get("DETECTOR_NAME", "LOG_DETECTOR")
        # Start from the detector's defaults so missing keys keep sane values.
        params["detector"] = {"DETECTOR_NAME": name, **P.TRACKMATE_DETECTORS.get(name, {}), **det}

    trk_el = settings.find("TrackerSettings")
    if trk_el is not None:
        trk: dict = {}
        _flatten_settings(trk_el, trk)
        trk.pop("trackersettingsclass", None)
        name = trk.get("TRACKER_NAME", "SPARSE_LAP_TRACKER")
        params["tracker"] = {"TRACKER_NAME": name, **P.TRACKMATE_TRACKERS.get(name, {}), **trk}

    init = settings.find("InitialSpotFilter")
    if init is not None and "value" in init.attrib:
        params["initial_spot_filter"] = {
            "feature": init.attrib.get("feature", "QUALITY"),
            "value": float(init.attrib["value"]),
            "is_above": init.attrib.get("isabove", "true").lower() == "true",
        }
    params["spot_filters"] = _filters(settings.find("SpotFilterCollection"))
    params["track_filters"] = _filters(settings.find("TrackFilterCollection"))

    inputs: list[str] = []
    img = settings.find("ImageData")
    if img is not None:
        folder, fname = img.attrib.get("folder", ""), img.attrib.get("filename", "")
        if fname:
            inputs.append(str(Path(folder) / fname))
        # Keep the calibration that was used, as a hint (only applied if set).
        pw = img.attrib.get("pixelwidth")
        if pw:
            notes.append(
                "Original calibration: pixel %s x %s, z %s, dt %s"
                % (pw, img.attrib.get("pixelheight"), img.attrib.get("voxeldepth"), img.attrib.get("timeinterval"))
            )
    return PreviousRun("trackmate", params, inputs, notes)


# --------------------------------------------------------------------------
# u-track .mat
# --------------------------------------------------------------------------

def _to_python(obj: Any) -> Any:
    """Convert scipy.io.loadmat output (squeeze_me, struct_as_record=False) to JSON-able Python."""
    import numpy as np
    from scipy.io.matlab import mat_struct

    if isinstance(obj, mat_struct):
        return {name: _to_python(getattr(obj, name)) for name in obj._fieldnames}
    if isinstance(obj, np.ndarray):
        if obj.dtype == object:
            return [_to_python(x) for x in obj.ravel(order="F")]
        if obj.dtype.kind in "US":
            return str(obj) if obj.ndim == 0 else "".join(obj.ravel().tolist())
        if obj.size == 0:
            return None
        if obj.size == 1:
            return _to_python(obj.reshape(-1)[0])
        return [_to_python(x) for x in obj.ravel(order="F")]
    if isinstance(obj, np.bool_):
        return bool(obj)
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, (float, np.floating)):
        # MATLAB stores everything as double; show whole numbers as ints.
        f = float(obj)
        return int(f) if f.is_integer() and abs(f) < 2**31 else f
    if isinstance(obj, bytes):
        return obj.decode("utf-8", "replace")
    return obj


_UTRACK_KEYS = ("detectionParam", "movieParam", "costMatrices", "gapCloseParam", "kalmanFunctions", "probDim")


def _search_utrack_structs(data: Any, found: dict | None = None) -> dict:
    """Recursively look for u-track parameter structs anywhere in ``data``."""
    if found is None:
        found = {}
    if isinstance(data, dict):
        for k, v in data.items():
            if k in _UTRACK_KEYS and k not in found and v is not None:
                found[k] = v
            else:
                _search_utrack_structs(v, found)
        # pointSourceDetection params live in PointSourceDetectionProcess.funParams_
        if "pointSource" not in found and ("alpha" in data or "Alpha" in data) and "Mode" in data:
            found["pointSource"] = data
    elif isinstance(data, list):
        for v in data:
            _search_utrack_structs(v, found)
    return found


def _utrack_from_found(found: dict, notes: list[str]) -> PreviousRun:
    params = P.utrack_defaults()
    det = params["detection"]
    if "detectionParam" in found and isinstance(found["detectionParam"], dict):
        dp = dict(found["detectionParam"])
        # Drop file-system specific bits from the old machine.
        for k in ("background", "maskLoc"):
            if k in dp:
                notes.append(f"Ignoring detectionParam.{k} from previous run (machine specific)")
                dp.pop(k)
        det["detectionParam"] = P.deep_merge(det["detectionParam"], dp)
        det["method"] = "subres_gaussian"
    if "pointSource" in found:
        ps = found["pointSource"]
        opts = det["pointSource"]
        mapping = {"alpha": "Alpha", "mode": "Mode", "fitmixtures": "FitMixtures", "maxmixtures": "MaxMixtures",
                   "redundancyradius": "RedundancyRadius", "prefilter": "Prefilter", "refinemasklog": "RefineMaskLoG",
                   "refinemaskvalid": "RefineMaskValid", "confradius": "ConfRadius", "windowsize": "WindowSize",
                   "removeredundant": "RemoveRedundant"}
        for k, v in ps.items():
            if k.lower() in mapping and v is not None:
                opts[mapping[k.lower()]] = v
        sigma = ps.get("filterSigma", ps.get("sigma"))
        if isinstance(sigma, list):
            sigma = sigma[0] if sigma else None
        if sigma:
            opts["sigma"] = sigma
        if "detectionParam" not in found:
            det["method"] = "point_source"
    if isinstance(found.get("movieParam"), dict):
        mp = found["movieParam"]
        if mp.get("firstImageNum"):
            params["image"]["first_frame"] = mp["firstImageNum"]
    trk = params["tracking"]
    if isinstance(found.get("gapCloseParam"), dict):
        trk["gapCloseParam"] = P.deep_merge(trk["gapCloseParam"], found["gapCloseParam"])
    if found.get("costMatrices"):
        cm = found["costMatrices"]
        trk["costMatrices"] = cm if isinstance(cm, list) else [cm]
    if isinstance(found.get("kalmanFunctions"), dict):
        trk["kalmanFunctions"] = found["kalmanFunctions"]
    if found.get("probDim"):
        trk["probDim"] = int(found["probDim"])
    used = [k for k in found if k in _UTRACK_KEYS or k == "pointSource"]
    notes.append("Used: " + ", ".join(used) if used else "No u-track parameters found; using defaults")
    return PreviousRun("utrack", params, [], notes)


def load_utrack_mat(path: str | Path) -> PreviousRun:
    path = Path(path)
    from scipy.io import loadmat

    try:
        raw = loadmat(path, squeeze_me=True, struct_as_record=False)
    except NotImplementedError as exc:  # v7.3 (HDF5) files
        raise ValueError(
            f"{path.name} is a MATLAB v7.3 file. Export it first with "
            "'trackbatch params --matlab-export' (requires MATLAB)."
        ) from exc
    except Exception as exc:
        # MovieData objects (classdef) cannot be read without MATLAB.
        raise ValueError(
            f"Could not read {path.name} without MATLAB ({exc}). If this is a u-track 2.x "
            "movieData.mat, use 'trackbatch params --matlab-export'."
        ) from exc
    data = {k: _to_python(v) for k, v in raw.items() if not k.startswith("__")}
    found = _search_utrack_structs(data)
    if not found:
        raise ValueError(
            f"No u-track parameters in {path.name}. If this is a u-track 2.x movieData.mat, "
            "use 'trackbatch params --matlab-export' (requires MATLAB)."
        )
    return _utrack_from_found(found, [f"Loaded u-track parameters from {path.name}"])


def matlab_export_params(mat_path: str | Path, matlab: str | None, utrack_path: str | None) -> PreviousRun:
    """Use MATLAB to read files scipy cannot (u-track 2.x movieData.mat, v7.3 files)."""
    import subprocess
    import tempfile
    from importlib import resources

    from . import tools

    exe = tools.resolve_matlab(matlab)
    if exe is None:
        raise ValueError("MATLAB not found - pass --matlab or set it with 'trackbatch config --matlab'")
    mat_path = Path(mat_path).resolve()
    with tempfile.TemporaryDirectory() as tmp:
        script = resources.files("trackbatch").joinpath("resources", "trackbatch_export_params.m").read_text("utf-8")
        Path(tmp, "trackbatch_export_params.m").write_text(script, encoding="utf-8")
        out = Path(tmp, "export.json")

        def q(s: str) -> str:
            return "'" + str(s).replace("'", "''") + "'"

        stmt = f"addpath({q(tmp)}); trackbatch_export_params({q(mat_path)}, {q(out)}, {q(utrack_path or '')});"
        proc = subprocess.run([str(exe), "-batch", stmt], capture_output=True, text=True)
        if proc.returncode != 0 or not out.exists():
            raise ValueError(f"MATLAB export failed:\n{proc.stdout}\n{proc.stderr}")
        data = json.loads(out.read_text(encoding="utf-8"))
    found = _search_utrack_structs(data)
    return _utrack_from_found(found, [f"Exported u-track parameters from {mat_path.name} via MATLAB"])
