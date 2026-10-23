import json
import os
import platform
import stat
import sys
from pathlib import Path

import pytest

from trackbatch import params as P
from trackbatch.batch import BatchConfig, build_jobs, discover_inputs, run_batch
from trackbatch.cli import main as cli_main
from trackbatch.previous import load_previous


def _images(tmp_path, names):
    import numpy as np
    import tifffile

    d = tmp_path / "imgs"
    d.mkdir()
    for n in names:
        if n.lower().endswith((".tif", ".tiff")):
            tifffile.imwrite(d / n, np.zeros((3, 8, 8), np.uint16), imagej=True, resolution=(10, 10),
                             metadata={"axes": "TYX", "unit": "um"})
        else:
            (d / n).write_bytes(b"x")
    return d


def test_discover_and_jobs(tmp_path):
    d = _images(tmp_path, ["a.tif", "b.TIFF", "notes.txt"])
    sub = d / "sub"
    sub.mkdir()
    (sub / "a.tif").write_bytes(b"x")
    found = discover_inputs([str(d)])
    assert [p.name for p in found] == ["a.tif", "b.TIFF"]
    found = discover_inputs([str(d)], recursive=True)
    assert len(found) == 3
    jobs = build_jobs(BatchConfig("trackmate", {}, [str(p) for p in found], str(tmp_path / "out")))
    names = sorted(Path(j.output_dir).name for j in jobs)
    assert names == ["a", "a_2", "b"]  # duplicate stems don't collide
    with pytest.raises(FileNotFoundError):
        discover_inputs([str(tmp_path / "missing")])


@pytest.mark.parametrize("engine", ["trackmate", "utrack"])
def test_dry_run_writes_scripts_and_manifests(tmp_path, engine):
    d = _images(tmp_path, ["a.tif", "b.tif", "c.tif"])
    out = tmp_path / "out"
    logs = []
    cfg = BatchConfig(engine, P.defaults(engine), [str(p) for p in discover_inputs([str(d)])], str(out),
                      fiji_path="/opt/Fiji.app/ImageJ-linux64", matlab_path="/opt/matlab", workers=2, dry_run=True)
    run_batch(cfg, log=logs.append)
    work = out / "_trackbatch"
    manifests = sorted(work.glob("manifest_*.json"))
    assert len(manifests) == 2
    m = json.loads(manifests[0].read_text())
    assert m["engine"] == engine and len(m["jobs"]) == 2
    if engine == "trackmate":
        assert (work / "trackmate_batch.py").exists()
        assert any("--headless" in line for line in logs)
    else:
        assert (work / "trackbatch_utrack_batch.m").exists()
        assert any("-batch" in line for line in logs)
        assert "Infinity" not in manifests[0].read_text()  # MATLAB jsondecode can't read it
    # every image folder has a reusable copy of the parameters
    assert load_previous(out / "a" / "trackbatch_params.json").engine == engine
    assert (out / "trackbatch_run.json").exists()
    assert "[dry-run] worker 1" in (work / "batch.log").read_text()


FAKE_FIJI = r'''#!PYTHON
import json, os, sys
m = json.load(open(os.environ["TRACKBATCH_MANIFEST"]))
assert "--headless" in sys.argv
for job in m["jobs"]:
    print("[trackbatch] JOB %d START %s" % (job["id"], job["image"]), flush=True)
    bad = "bad" in job["image"]
    st = {"status": "failed" if bad else "ok", "error": "boom" if bad else "",
          "stats": {} if bad else {"n_spots": 10, "n_tracks": 2}}
    json.dump(st, open(os.path.join(job["output_dir"], "trackbatch_status.json"), "w"))
    print("[trackbatch] JOB %d %s" % (job["id"], "FAILED" if bad else "OK"), flush=True)
'''


@pytest.mark.skipif(platform.system() == "Windows", reason="fake launcher uses a shebang")
def test_end_to_end_with_fake_fiji(tmp_path):
    fake = tmp_path / "fiji-linux-x64"
    fake.write_text(FAKE_FIJI.replace("PYTHON", sys.executable))
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    d = _images(tmp_path, ["good1.tif", "bad.tif", "good2.tif"])
    out = tmp_path / "out"
    code = cli_main(["run", "-e", "trackmate", "-i", str(d), "-o", str(out), "--fiji", str(fake),
                     "-w", "2", "--set", "detector.RADIUS=4"])
    assert code == 1  # one image failed
    run = json.loads((out / "trackbatch_run.json").read_text())
    status = {Path(j["image"]).name: j["status"] for j in run["jobs"]}
    assert status == {"bad.tif": "failed", "good1.tif": "ok", "good2.tif": "ok"}
    assert run["params"]["detector"]["RADIUS"] == 4
    summary = (out / "batch_summary.csv").read_text()
    assert "n_spots" in summary and "boom" in summary
    batch_log = (out / "_trackbatch" / "batch.log").read_text()
    assert "exited with code 0" in batch_log and "[trackbatch] JOB" in batch_log

    # Re-run with the previous run's parameters, skipping finished images.
    code = cli_main(["run", "--from-previous", str(out / "trackbatch_run.json"), "--reuse-inputs",
                     "-o", str(out), "--fiji", str(fake), "--skip-existing"])
    run = json.loads((out / "trackbatch_run.json").read_text())
    status = {Path(j["image"]).name: j["status"] for j in run["jobs"]}
    assert status == {"bad.tif": "failed", "good1.tif": "skipped", "good2.tif": "skipped"}
    assert run["params"]["detector"]["RADIUS"] == 4


def test_missing_tool_reported(tmp_path, capsys):
    d = _images(tmp_path, ["a.tif"])
    code = cli_main(["run", "-e", "trackmate", "-i", str(d), "-o", str(tmp_path / "o"),
                     "--fiji", str(tmp_path / "nope")])
    assert code == 2
    assert "Fiji not found" in capsys.readouterr().err


def test_params_command(tmp_path, capsys):
    xml = Path(__file__).parent / "data" / "previous_trackmate.xml"
    out = tmp_path / "p.json"
    assert cli_main(["params", "--from-previous", str(xml), "--set", "detector.THRESHOLD=20", "-o", str(out)]) == 0
    data = json.loads(out.read_text())
    assert data["engine"] == "trackmate"
    assert data["params"]["detector"]["THRESHOLD"] == 20
    assert data["params"]["detector"]["DETECTOR_NAME"] == "DOG_DETECTOR"


def test_run_stops_on_precheck_errors(tmp_path, capsys):
    d = _images(tmp_path, ["a.tif"])
    (d / "broken.tif").write_bytes(b"II*\x00junk")
    out = tmp_path / "out"
    code = cli_main(["run", "-e", "trackmate", "-i", str(d), "-o", str(out), "--fiji", str(tmp_path / "nope")])
    assert code == 2
    assert "pre-check found 1 problem" in capsys.readouterr().err
    assert "broken.tif" in (out / "precheck.txt").read_text()
    assert not (out / "_trackbatch").exists()  # nothing was started
    # --no-precheck goes on to the tool check instead
    code = cli_main(["run", "-e", "trackmate", "-i", str(d), "-o", str(out), "--fiji", str(tmp_path / "nope"),
                     "--no-precheck"])
    assert code == 2 and "Fiji not found" in capsys.readouterr().err


@pytest.mark.skipif(platform.system() == "Windows", reason="fake launcher uses a shebang")
def test_silent_worker_is_flagged(tmp_path):
    fake = tmp_path / "fiji-linux-x64"
    fake.write_text("#!/bin/sh\necho 'Fiji says hello but never runs the script'\nexit 3\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    d = _images(tmp_path, ["a.tif"])
    out = tmp_path / "out"
    code = cli_main(["run", "-e", "trackmate", "-i", str(d), "-o", str(out), "--fiji", str(fake)])
    assert code == 1
    batch_log = (out / "_trackbatch" / "batch.log").read_text()
    assert "exited with code 3" in batch_log and "no TrackBatch progress lines" in batch_log
    run = json.loads((out / "trackbatch_run.json").read_text())
    assert "exited with code 3" in run["jobs"][0]["error"]
