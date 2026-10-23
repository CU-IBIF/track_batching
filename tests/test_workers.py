"""Exercise the Fiji (Jython) and MATLAB worker scripts against stubbed TrackMate / u-track.

These need extra runtimes and are skipped when they're missing:
  * Jython: set JYTHON_JAR to a jython-standalone-2.7.x jar (CI downloads it).
  * Octave >= 7 stands in for MATLAB.
"""
import csv
import json
import os
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

from trackbatch import params as P
from trackbatch.batch import BatchConfig, run_batch

STUBS = Path(__file__).parent / "stubs"
JYTHON_JAR = os.environ.get("JYTHON_JAR")


def _dry_run(tmp_path, engine, params, images):
    out = tmp_path / "out"
    cfg = BatchConfig(engine, params, [str(i) for i in images], str(out), dry_run=True)
    run_batch(cfg, log=lambda m: None)
    return out, out / "_trackbatch", out / "_trackbatch" / "manifest_0.json"


@pytest.mark.skipif(not (JYTHON_JAR and shutil.which("java")), reason="needs java + JYTHON_JAR")
def test_trackmate_worker_under_jython(tmp_path):
    imgs = [tmp_path / "a.tif", tmp_path / "broken.tif"]
    for i in imgs:
        i.write_bytes(b"")
    params = P.apply_overrides(P.defaults("trackmate"), [
        "detector.RADIUS=3",  # int on purpose: must reach TrackMate as a Double
        'spot_filters=[{"feature": "QUALITY", "value": 2, "is_above": true}]',
    ])
    out, work, manifest = _dry_run(tmp_path, "trackmate", params, imgs)
    proc = subprocess.run(
        ["java", f"-Dpython.path={STUBS / 'jython'}", "-jar", JYTHON_JAR, str(work / "trackmate_batch.py")],
        env=dict(os.environ, TRACKBATCH_MANIFEST=str(manifest)), capture_output=True, text=True, timeout=300,
    )
    print(proc.stdout, proc.stderr)
    good = json.loads((out / "a" / "trackbatch_status.json").read_text())
    bad = json.loads((out / "broken" / "trackbatch_status.json").read_text())
    assert good["status"] == "ok", good
    assert good["stats"]["n_spots"] == 4 and good["stats"]["n_tracks"] == 1
    assert bad["status"] == "failed" and "Could not open" in bad["error"]
    for name in ("spots.csv", "edges.csv", "tracks.csv", "trackmate.xml"):
        assert (out / "a" / name).exists(), name
    assert "RADIUS': 3.0" in proc.stdout  # coerced to float


@pytest.mark.skipif(shutil.which("octave") is None, reason="needs GNU Octave")
@pytest.mark.parametrize("method", ["subres_gaussian", "point_source"])
def test_utrack_worker_under_octave(tmp_path, method):
    tifffile = pytest.importorskip("tifffile")
    # ImageJ hyperstack: 6 frames, 2 channels; a bright spot moves along x in channel 2.
    stack = np.zeros((6, 2, 32, 32), dtype=np.uint16)
    for t in range(6):
        stack[t, 1, 10, 5 + t] = 1000
    img = tmp_path / "movie.tif"
    tifffile.imwrite(img, stack, imagej=True, metadata={"axes": "TCYX"})

    params = P.apply_overrides(P.defaults("utrack"), [
        f"detection.method={method}", "image.channel=2", "image.first_frame=2",
    ])
    out, work, manifest = _dry_run(tmp_path, "utrack", params, [img])
    stmt = f"addpath('{work}'); addpath('{STUBS / 'utrack'}'); trackbatch_utrack_batch('{manifest}');"
    proc = subprocess.run(["octave", "--no-gui", "--quiet", "--eval", stmt],
                          capture_output=True, text=True, timeout=300)
    print(proc.stdout, proc.stderr)
    status = json.loads((out / "movie" / "trackbatch_status.json").read_text())
    assert status["status"] == "ok", status
    assert status["stats"]["n_frames"] == 5
    dets = list(csv.DictReader((out / "movie" / "detections.csv").open()))
    first = [d for d in dets if d["FRAME"] == "2"][0]
    assert float(first["X"]) == 7 and float(first["Y"]) == 11  # 1-based pixel of frame 2, channel 2
    rows = list(csv.DictReader((out / "movie" / "tracks.csv").open()))
    assert {r["FRAME"] for r in rows if r["TRACK_ID"] == "1"} == {"2", "3", "4", "5", "6"}
    assert (out / "movie" / "tracking.mat").exists()
    assert not (out / "movie" / "frames_tmp").exists()
