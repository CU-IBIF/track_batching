import numpy as np
import pytest
import tifffile

from trackbatch import params as P
from trackbatch.cli import main as cli_main
from trackbatch.precheck import inspect_image, precheck


def _hyperstack(path, shape, axes, dtype=np.uint16, px=None, finterval=None):
    md = {"axes": axes}
    kw = {}
    if px:
        md["unit"] = "um"
        kw["resolution"] = (1 / px, 1 / px)
    if finterval:
        md["finterval"] = finterval
    tifffile.imwrite(path, np.zeros(shape, dtype), imagej=True, metadata=md, **kw)
    return str(path)


@pytest.fixture
def mixed(tmp_path):
    return {
        "big": _hyperstack(tmp_path / "big.tif", (10, 2, 64, 80), "TCYX", px=0.108, finterval=0.05),
        "small": _hyperstack(tmp_path / "small.tif", (12, 32, 32), "TYX", np.uint8, px=0.16, finterval=0.1),
        "zstack": _hyperstack(tmp_path / "zstack.tif", (7, 16, 16), "ZYX"),
        "z3d": _hyperstack(tmp_path / "z3d.tif", (4, 3, 16, 16), "TZYX", px=0.108),
    }


def _messages(res, path=None):
    return [i.message for i in res.issues if i.image == path]


def test_inspect_reads_header(mixed, tmp_path):
    im = inspect_image(mixed["big"])
    assert (im.width, im.height, im.channels, im.slices, im.frames) == (80, 64, 2, 1, 10)
    assert im.bit_depth == 16 and im.pixel_size == pytest.approx(0.108) and im.unit == "µm"
    assert im.frame_interval == pytest.approx(0.05)
    assert inspect_image(mixed["zstack"]).pixel_size is None

    ome = tmp_path / "x.ome.tif"
    tifffile.imwrite(ome, np.zeros((5, 3, 20, 30), np.uint16),
                     metadata={"axes": "TCYX", "PhysicalSizeX": 0.2, "PhysicalSizeXUnit": "µm"})
    im = inspect_image(ome)
    assert (im.width, im.channels, im.frames, im.pixel_size) == (30, 3, 5, pytest.approx(0.2))

    plain = tmp_path / "plain.tif"
    tifffile.imwrite(plain, np.zeros((6, 10, 10), np.float32))
    im = inspect_image(plain)
    assert im.frames == 6 and im.is_float

    broken = tmp_path / "broken.tif"
    broken.write_bytes(b"II*\x00junk")
    assert not inspect_image(broken).readable
    other = tmp_path / "m.nd2"
    other.write_bytes(b"x")
    im = inspect_image(other)
    assert im.readable and not im.inspected


def test_trackmate_checks(mixed):
    p = P.apply_overrides(P.defaults("trackmate"), ["detector.TARGET_CHANNEL=2"])
    res = precheck("trackmate", p, list(mixed.values()))
    assert res.status_of(mixed["big"]) == "ok"
    assert any("channel 2 requested" in m for m in _messages(res, mixed["small"]))
    assert any("treated as 7 time points" in m for m in _messages(res, mixed["zstack"]))
    assert any("no pixel calibration" in m for m in _messages(res, mixed["zstack"]))
    assert not any("2D only" in m for m in _messages(res, mixed["z3d"]))  # TrackMate does 3D
    batch = " ".join(_messages(res))
    assert "image sizes differ" in batch and "TrackMate works in physical units" in batch
    assert "bit depths differ" in batch and "frame intervals differ" in batch


def test_trackmate_calibration_override(mixed):
    p = P.apply_overrides(P.defaults("trackmate"), ["calibration.pixel_width=0.1"])
    res = precheck("trackmate", p, [mixed["big"], mixed["small"], mixed["zstack"]])
    assert any("calibration override applies one pixel size" in m for m in _messages(res))
    assert not any("no pixel calibration" in m for m in _messages(res, mixed["zstack"]))


def test_utrack_checks(mixed):
    p = P.apply_overrides(P.defaults("utrack"), ["image.first_frame=11"])
    res = precheck("utrack", p, list(mixed.values()))
    assert any("2D only" in m for m in _messages(res, mixed["z3d"]))
    assert any("first_frame=11" in m for m in _messages(res, mixed["big"]))
    assert not any("first_frame" in m for m in _messages(res, mixed["small"]))  # 12 frames
    assert any("8-bit image but detectionParam.bitDepth=16" in m for m in _messages(res, mixed["small"]))
    assert any("u-track parameters are in pixels" in m for m in _messages(res))


def test_same_size_batch_is_clean(tmp_path):
    imgs = [_hyperstack(tmp_path / f"m{i}.tif", (5, 16, 16), "TYX", px=0.1) for i in range(3)]
    res = precheck("trackmate", P.defaults("trackmate"), imgs)
    assert res.issues == []
    assert "No problems found" in res.report()


def test_check_command(mixed, capsys):
    assert cli_main(["check", "-e", "trackmate", "-i", mixed["big"], "-i", mixed["small"]]) == 0
    out = capsys.readouterr().out
    assert "big.tif" in out and "80x64" in out
    assert cli_main(["check", "-e", "utrack", "-i", mixed["z3d"]]) == 1
