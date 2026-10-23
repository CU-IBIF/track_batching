import json
import math
from pathlib import Path

import numpy as np
import pytest
from scipy.io import savemat

from trackbatch import params as P
from trackbatch.previous import load_previous

DATA = Path(__file__).parent / "data"


def test_trackmate_xml():
    prev = load_previous(DATA / "previous_trackmate.xml")
    assert prev.engine == "trackmate"
    det, trk = prev.params["detector"], prev.params["tracker"]
    assert det["DETECTOR_NAME"] == "DOG_DETECTOR"
    assert det["TARGET_CHANNEL"] == 2 and det["RADIUS"] == 0.35 and det["DO_MEDIAN_FILTERING"] is True
    assert trk["TRACKER_NAME"] == "SPARSE_LAP_TRACKER"
    assert trk["LINKING_MAX_DISTANCE"] == 0.8
    assert trk["MAX_FRAME_GAP"] == 3
    assert trk["ALLOW_TRACK_MERGING"] is True
    assert trk["BLOCKING_VALUE"] == math.inf
    assert trk["LINKING_FEATURE_PENALTIES"] == {"MEAN_INTENSITY_CH2": 0.5}
    assert trk["GAP_CLOSING_FEATURE_PENALTIES"] == {}
    assert prev.params["initial_spot_filter"] == {"feature": "QUALITY", "value": 3.0, "is_above": True}
    assert prev.params["spot_filters"][0]["feature"] == "MEAN_INTENSITY_CH2"
    assert prev.params["track_filters"] == [{"feature": "NUMBER_SPOTS", "value": 5.0, "is_above": True}]
    assert prev.inputs == [str(Path("C:/data/exp1/") / "cells_01.tif")]


def _utrack_tracking_mat(path):
    params = {"linearMotion": 1.0, "minSearchRadius": 1.0, "maxSearchRadius": 4.0, "brownStdMult": 3.0,
              "useLocalDensity": 1.0, "nnWindow": 4.0, "kalmanInitParam": np.zeros((0, 0)),
              "diagnostics": np.zeros((0, 0))}
    gap = {"linearMotion": 1.0, "minSearchRadius": 1.0, "maxSearchRadius": 4.0,
           "brownStdMult": 3.0 * np.ones((4, 1)), "brownScaling": np.array([[0.25, 0.01]]),
           "gapPenalty": 2.0, "resLimit": np.zeros((0, 0))}
    cm = np.zeros((1, 2), dtype=[("funcName", object), ("parameters", object)])
    cm[0, 0] = ("costMatRandomDirectedSwitchingMotionLink", params)
    cm[0, 1] = ("costMatRandomDirectedSwitchingMotionCloseGaps", gap)
    savemat(path, {
        "costMatrices": cm,
        "gapCloseParam": {"timeWindow": 4.0, "mergeSplit": 1.0, "minTrackLen": 3.0, "diagnostics": 0.0},
        "kalmanFunctions": {"reserveMem": "kalmanResMemLM", "initialize": "kalmanInitLinearMotion",
                            "calcGain": "kalmanGainLinearMotion", "timeReverse": "kalmanReverseLinearMotion"},
        "tracksFinal": np.zeros((0, 0)),
    })


def test_utrack_tracking_mat(tmp_path):
    mat = tmp_path / "tracking.mat"
    _utrack_tracking_mat(mat)
    prev = load_previous(mat)
    assert prev.engine == "utrack"
    trk = prev.params["tracking"]
    assert trk["gapCloseParam"] == {"timeWindow": 4, "mergeSplit": 1, "minTrackLen": 3, "diagnostics": 0}
    assert isinstance(trk["gapCloseParam"]["timeWindow"], int)
    link, gaps = trk["costMatrices"]
    assert link["funcName"] == "costMatRandomDirectedSwitchingMotionLink"
    assert link["parameters"]["maxSearchRadius"] == 4
    assert link["parameters"]["kalmanInitParam"] is None
    assert gaps["parameters"]["brownStdMult"] == [3, 3, 3, 3]
    assert gaps["parameters"]["brownScaling"] == [0.25, 0.01]
    assert trk["kalmanFunctions"]["initialize"] == "kalmanInitLinearMotion"
    # detection untouched -> defaults
    assert prev.params["detection"] == P.utrack_defaults()["detection"]


def test_utrack_detection_mat(tmp_path):
    mat = tmp_path / "detection.mat"
    savemat(mat, {
        "movieParam": {"imageDir": "/old/", "filenameBase": "img", "firstImageNum": 3.0, "lastImageNum": 50.0,
                       "digits4Enum": 4.0},
        "detectionParam": {"psfSigma": 1.8, "bitDepth": 12.0, "alphaLocMax": 0.01, "maskLoc": "/old/mask.tif",
                           "testAlpha": {"alphaR": 0.01, "alphaA": 0.02, "alphaD": 0.03, "alphaF": 0.0}},
    })
    prev = load_previous(mat)
    dp = prev.params["detection"]["detectionParam"]
    assert dp["psfSigma"] == 1.8 and dp["bitDepth"] == 12
    assert dp["testAlpha"]["alphaD"] == 0.03
    assert "maskLoc" not in dp
    assert prev.params["image"]["first_frame"] == 3
    assert any("maskLoc" in n for n in prev.notes)


def test_point_source_export_json(tmp_path):
    # Shape of `trackbatch params --matlab-export` output for a u-track 2.x movieData.mat
    export = {"MD": {"processes": [
        {"class": "PointSourceDetectionProcess",
         "funParams": {"ChannelIndex": 1, "alpha": 0.01, "Mode": "xyAc", "filterSigma": [1.3, 1.3],
                       "FitMixtures": False, "MaxMixtures": 5, "RedundancyRadius": 0.25}},
        {"class": "TrackingProcess",
         "funParams": {"gapCloseParam": {"timeWindow": 3, "mergeSplit": 0, "minTrackLen": 1, "diagnostics": 0},
                       "probDim": 2}},
    ]}}
    path = tmp_path / "export.json"
    path.write_text(json.dumps(export))
    prev = load_previous(path)
    assert prev.engine == "utrack"
    assert prev.params["detection"]["method"] == "point_source"
    ps = prev.params["detection"]["pointSource"]
    assert ps["Alpha"] == 0.01 and ps["sigma"] == 1.3 and ps["RedundancyRadius"] == 0.25
    assert prev.params["tracking"]["gapCloseParam"]["timeWindow"] == 3


def test_trackbatch_run_json_roundtrip(tmp_path):
    params = P.apply_overrides(P.defaults("trackmate"), ["detector.RADIUS=4"])
    run = {"engine": "trackmate", "params": params, "jobs": [{"image": "/x/a.tif"}, {"image": "/x/b.tif"}]}
    path = tmp_path / "trackbatch_run.json"
    path.write_text(P.dumps(run))
    prev = load_previous(path)
    assert prev.params["detector"]["RADIUS"] == 4
    assert prev.params["tracker"]["BLOCKING_VALUE"] == math.inf
    assert prev.inputs == ["/x/a.tif", "/x/b.tif"]


def test_unknown_suffix(tmp_path):
    with pytest.raises(ValueError):
        load_previous(tmp_path / "x.csv")
