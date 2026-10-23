"""Parameter sets for TrackMate and u-track, plus helpers to merge/override them.

Parameters are plain nested dicts so they can be round-tripped through JSON,
handed to the Fiji (Jython) and MATLAB worker scripts unchanged, and edited
generically in the GUI.
"""
from __future__ import annotations

import copy
import json
import math
from pathlib import Path
from typing import Any

ENGINES = ("trackmate", "utrack")

# --------------------------------------------------------------------------
# TrackMate
# --------------------------------------------------------------------------
# Keys mirror TrackMate's own settings maps (DetectorKeys / TrackerKeys), so a
# value read from a TrackMate XML file can be passed straight through.

TRACKMATE_DETECTORS: dict[str, dict[str, Any]] = {
    "LOG_DETECTOR": {
        "TARGET_CHANNEL": 1,
        "RADIUS": 2.5,
        "THRESHOLD": 0.0,
        "DO_MEDIAN_FILTERING": False,
        "DO_SUBPIXEL_LOCALIZATION": True,
    },
    "DOG_DETECTOR": {
        "TARGET_CHANNEL": 1,
        "RADIUS": 2.5,
        "THRESHOLD": 0.0,
        "DO_MEDIAN_FILTERING": False,
        "DO_SUBPIXEL_LOCALIZATION": True,
    },
    "HESSIAN_DETECTOR": {
        "TARGET_CHANNEL": 1,
        "RADIUS": 2.5,
        "RADIUS_Z": 2.5,
        "THRESHOLD": 0.0,
        "NORMALIZE": False,
        "DO_SUBPIXEL_LOCALIZATION": True,
    },
    "THRESHOLD_DETECTOR": {
        "TARGET_CHANNEL": 1,
        "INTENSITY_THRESHOLD": 0.0,
        "SIMPLIFY_CONTOURS": True,
    },
    "MASK_DETECTOR": {"TARGET_CHANNEL": 1, "SIMPLIFY_CONTOURS": True},
    "LABEL_IMAGE_DETECTOR": {"TARGET_CHANNEL": 1, "SIMPLIFY_CONTOURS": True},
}

_LAP_TRACKER = {
    "LINKING_MAX_DISTANCE": 15.0,
    "LINKING_FEATURE_PENALTIES": {},
    "ALLOW_GAP_CLOSING": True,
    "GAP_CLOSING_MAX_DISTANCE": 15.0,
    "MAX_FRAME_GAP": 2,
    "GAP_CLOSING_FEATURE_PENALTIES": {},
    "ALLOW_TRACK_SPLITTING": False,
    "SPLITTING_MAX_DISTANCE": 15.0,
    "SPLITTING_FEATURE_PENALTIES": {},
    "ALLOW_TRACK_MERGING": False,
    "MERGING_MAX_DISTANCE": 15.0,
    "MERGING_FEATURE_PENALTIES": {},
    "CUTOFF_PERCENTILE": 0.9,
    "ALTERNATIVE_LINKING_COST_FACTOR": 1.05,
    "BLOCKING_VALUE": math.inf,
}

TRACKMATE_TRACKERS: dict[str, dict[str, Any]] = {
    "SPARSE_LAP_TRACKER": dict(_LAP_TRACKER),
    "SIMPLE_SPARSE_LAP_TRACKER": {
        "LINKING_MAX_DISTANCE": 15.0,
        "GAP_CLOSING_MAX_DISTANCE": 15.0,
        "MAX_FRAME_GAP": 2,
    },
    "KALMAN_TRACKER": {
        "LINKING_MAX_DISTANCE": 15.0,
        "KALMAN_SEARCH_RADIUS": 15.0,
        "MAX_FRAME_GAP": 2,
    },
    "NEAREST_NEIGHBOR_TRACKER": {"LINKING_MAX_DISTANCE": 15.0},
    "OVERLAP_TRACKER": {
        "SCALE_FACTOR": 1.0,
        "MIN_IOU": 0.3,
        "IOU_CALCULATION": "PRECISE",
    },
}


def trackmate_defaults(detector: str = "LOG_DETECTOR", tracker: str = "SPARSE_LAP_TRACKER") -> dict:
    det = {"DETECTOR_NAME": detector}
    det.update(copy.deepcopy(TRACKMATE_DETECTORS.get(detector, {})))
    trk = {"TRACKER_NAME": tracker}
    trk.update(copy.deepcopy(TRACKMATE_TRACKERS.get(tracker, {})))
    return {
        "detector": det,
        # Only QUALITY is allowed as the initial filter in TrackMate.
        "initial_spot_filter": None,  # e.g. {"feature": "QUALITY", "value": 1.0, "is_above": true}
        "spot_filters": [],           # list of {"feature", "value", "is_above"}
        "tracker": trk,
        "track_filters": [],
        # Override the image calibration (None = keep what's in the file).
        "calibration": {
            "pixel_width": None,
            "pixel_height": None,
            "voxel_depth": None,
            "frame_interval": None,
            "space_unit": None,
            "time_unit": None,
        },
        "image": {
            # Many TIFFs store time as Z; "auto" swaps when nframes==1 and nslices>1.
            "swap_z_t": "auto",
            "series": 0,
        },
        "export": {"xml": True, "spots_csv": True, "edges_csv": True, "tracks_csv": True},
        "threads": 0,  # 0 = all cores
    }


# --------------------------------------------------------------------------
# u-track
# --------------------------------------------------------------------------
# Structs mirror u-track's scriptDetectGeneral.m / scriptTrackGeneral.m so
# values loaded from a previous run's .mat file map 1:1.

def utrack_defaults() -> dict:
    time_window = 5
    return {
        "detection": {
            # "subres_gaussian": detectSubResFeatures2D_StandAlone (classic u-track)
            # "point_source":    pointSourceDetection (u-track 2.x default in the GUI)
            "method": "subres_gaussian",
            "detectionParam": {
                "psfSigma": 1.5,
                "testAlpha": {"alphaR": 0.05, "alphaA": 0.05, "alphaD": 0.05, "alphaF": 0},
                "visual": 0,
                "doMMF": 0,
                "bitDepth": 16,
                "alphaLocMax": 0.05,
                "numSigmaIter": 0,
                "integWindow": 0,
                "calcMethod": "g",
            },
            # Name/value options for pointSourceDetection(img, sigma, ...)
            "pointSource": {
                "sigma": 1.5,
                "Alpha": 0.05,
                "Mode": "xyAc",
                "FitMixtures": False,
                "MaxMixtures": 5,
            },
        },
        "tracking": {
            "probDim": 2,
            "gapCloseParam": {
                "timeWindow": time_window,
                "mergeSplit": 0,
                "minTrackLen": 2,
                "diagnostics": 0,
            },
            "costMatrices": [
                {
                    "funcName": "costMatRandomDirectedSwitchingMotionLink",
                    "parameters": {
                        "linearMotion": 0,
                        "minSearchRadius": 2,
                        "maxSearchRadius": 5,
                        "brownStdMult": 3,
                        "useLocalDensity": 1,
                        "nnWindow": time_window,
                        "kalmanInitParam": None,
                        "diagnostics": None,
                    },
                },
                {
                    "funcName": "costMatRandomDirectedSwitchingMotionCloseGaps",
                    "parameters": {
                        "linearMotion": 0,
                        "minSearchRadius": 2,
                        "maxSearchRadius": 5,
                        # Scalars are expanded to timeWindow-long vectors in MATLAB.
                        "brownStdMult": 3,
                        "brownScaling": [0.25, 0.01],
                        "timeReachConfB": time_window,
                        "ampRatioLimit": [0.7, 4],
                        "lenForClassify": 5,
                        "useLocalDensity": 0,
                        "nnWindow": time_window,
                        "linStdMult": 1,
                        "linScaling": [0.25, 0.01],
                        "timeReachConfL": time_window,
                        "maxAngleVV": 30,
                        "gapPenalty": 1.5,
                        "resLimit": None,
                        "gapExcludeMS": 1,
                        "strategyBD": -1,
                    },
                },
            ],
            "kalmanFunctions": {
                "reserveMem": "kalmanResMemLM",
                "initialize": "kalmanInitLinearMotion",
                "calcGain": "kalmanGainLinearMotion",
                "timeReverse": "kalmanReverseLinearMotion",
            },
        },
        "image": {
            "channel": 1,        # 1-based channel to analyse
            "first_frame": 1,
            "last_frame": None,  # None = last frame of the movie
        },
        "export": {"detections_csv": True, "tracks_csv": True},
    }


def defaults(engine: str) -> dict:
    if engine == "trackmate":
        return trackmate_defaults()
    if engine == "utrack":
        return utrack_defaults()
    raise ValueError(f"Unknown engine {engine!r}; expected one of {ENGINES}")


# --------------------------------------------------------------------------
# Generic helpers
# --------------------------------------------------------------------------

def deep_merge(base: dict, update: dict) -> dict:
    """Return a copy of ``base`` with ``update`` merged in recursively.

    Lists and scalars from ``update`` replace those in ``base``.
    """
    out = copy.deepcopy(base)
    for key, value in update.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def parse_value(text: str) -> Any:
    """Parse a user-typed value: JSON first, then a few friendly spellings."""
    s = text.strip()
    low = s.lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    if low in ("none", "null", ""):
        return None
    if low in ("inf", "infinity", "+inf"):
        return math.inf
    if low in ("-inf", "-infinity"):
        return -math.inf
    try:
        return json.loads(s)
    except ValueError:
        return s


def set_path(params: dict, dotted: str, value: Any) -> None:
    """Set ``params[a][b][c] = value`` for ``dotted == "a.b.c"``.

    Integer components index into lists, e.g. ``tracking.costMatrices.1.parameters.maxSearchRadius``.
    """
    parts = dotted.split(".")
    node: Any = params
    for i, part in enumerate(parts[:-1]):
        if isinstance(node, list):
            node = node[int(part)]
            continue
        if part not in node or node[part] is None:
            node[part] = {}
        node = node[part]
    last = parts[-1]
    if isinstance(node, list):
        node[int(last)] = value
    else:
        node[last] = value


def apply_overrides(params: dict, overrides: list[str]) -> dict:
    """Apply ``key.path=value`` strings (as given with ``--set``)."""
    out = copy.deepcopy(params)
    for item in overrides:
        if "=" not in item:
            raise ValueError(f"Override {item!r} must look like key.path=value")
        key, value = item.split("=", 1)
        set_path(out, key.strip(), parse_value(value))
    return out


def dumps(obj: Any) -> str:
    # allow_nan keeps Infinity (TrackMate's BLOCKING_VALUE); both Python and
    # Jython's json modules read it back.
    return json.dumps(obj, indent=2, allow_nan=True, sort_keys=False)


def save_params(path: str | Path, engine: str, params: dict) -> None:
    Path(path).write_text(dumps({"engine": engine, "params": params}), encoding="utf-8")
