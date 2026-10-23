import math

import pytest

from trackbatch import params as P


def test_defaults_for_both_engines():
    tm = P.defaults("trackmate")
    assert tm["detector"]["DETECTOR_NAME"] == "LOG_DETECTOR"
    assert tm["tracker"]["BLOCKING_VALUE"] == math.inf
    ut = P.defaults("utrack")
    assert [c["funcName"] for c in ut["tracking"]["costMatrices"]] == [
        "costMatRandomDirectedSwitchingMotionLink",
        "costMatRandomDirectedSwitchingMotionCloseGaps",
    ]
    with pytest.raises(ValueError):
        P.defaults("nope")


def test_parse_value():
    assert P.parse_value("3") == 3
    assert P.parse_value("2.5") == 2.5
    assert P.parse_value("True") is True
    assert P.parse_value("null") is None
    assert P.parse_value("Infinity") == math.inf
    assert P.parse_value("[1, 2]") == [1, 2]
    assert P.parse_value("LOG_DETECTOR") == "LOG_DETECTOR"


def test_overrides_including_list_index():
    p = P.apply_overrides(P.defaults("utrack"), [
        "tracking.gapCloseParam.timeWindow=9",
        "tracking.costMatrices.1.parameters.maxSearchRadius=7.5",
        "detection.method=point_source",
    ])
    assert p["tracking"]["gapCloseParam"]["timeWindow"] == 9
    assert p["tracking"]["costMatrices"][1]["parameters"]["maxSearchRadius"] == 7.5
    assert p["detection"]["method"] == "point_source"
    with pytest.raises(ValueError):
        P.apply_overrides({}, ["no-equals-sign"])


def test_deep_merge_does_not_mutate():
    base = {"a": {"b": 1, "c": 2}}
    out = P.deep_merge(base, {"a": {"b": 5}})
    assert out == {"a": {"b": 5, "c": 2}}
    assert base == {"a": {"b": 1, "c": 2}}
