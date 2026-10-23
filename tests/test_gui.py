"""GUI smoke test (needs a display; CI runs pytest under xvfb-run)."""
import json
import os
import time
from pathlib import Path
from unittest import mock

import pytest

tk = pytest.importorskip("tkinter")
if not os.environ.get("DISPLAY") and os.name != "nt":
    pytest.skip("no display", allow_module_level=True)

from trackbatch import gui  # noqa: E402
from trackbatch import params as P  # noqa: E402

XML = Path(__file__).parent / "data" / "previous_trackmate.xml"


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(gui.tools, "CONFIG_DIR", tmp_path / "cfg")
    monkeypatch.setattr(gui.tools, "CONFIG_FILE", tmp_path / "cfg" / "settings.json")
    a = gui.App()
    a.update()
    yield a
    a.destroy()


def test_load_edit_and_dry_run(app, tmp_path):
    with mock.patch.object(gui.filedialog, "askopenfilename", return_value=str(XML)), \
         mock.patch.object(gui.messagebox, "askyesno", return_value=False), \
         mock.patch.object(gui.messagebox, "showinfo"):
        app._load_previous()
    app.update()
    assert app.engine.get() == "trackmate"
    assert app.detector.get() == "DOG_DETECTOR"

    # Edit a value through the tree's in-place editor.
    app.nb.select(1)
    app.update()
    tree = app.tree
    iid = next(i for i, p in tree.paths.items() if p == ["detector", "RADIUS"])
    tree.tree.see(iid)
    tree.tree.focus(iid)
    app.update()
    tree._begin_edit()
    editor = tree._editor
    editor.delete(0, "end")
    editor.insert(0, "0.5")
    editor.focus_force()
    app.update()
    editor.event_generate("<Return>")
    app.update()
    assert app.params["trackmate"]["detector"]["RADIUS"] == 0.5

    app._switch_component("tracker", "TRACKER_NAME", "KALMAN_TRACKER", P.TRACKMATE_TRACKERS)
    assert "KALMAN_SEARCH_RADIUS" in app.params["trackmate"]["tracker"]

    app.engine.set("utrack")
    app._engine_changed()
    assert app.det_method.get() == "subres_gaussian"
    app.engine.set("trackmate")
    app._engine_changed()

    img = tmp_path / "x.tif"
    img.write_bytes(b"")
    app._add_paths([str(img)])
    app.output.set(str(tmp_path / "out"))
    app.dry.set(True)
    app._run()
    t0 = time.time()
    while app.worker.is_alive() and time.time() - t0 < 20:
        app.update()
        time.sleep(0.05)
    app.update()
    m = json.loads((tmp_path / "out" / "_trackbatch" / "manifest_0.json").read_text())
    assert m["params"]["detector"]["RADIUS"] == 0.5
    assert m["params"]["tracker"]["TRACKER_NAME"] == "KALMAN_TRACKER"


def test_check_images_table_and_run_guard(app, tmp_path):
    import numpy as np
    import tifffile

    good = tmp_path / "good.tif"
    tifffile.imwrite(good, np.zeros((4, 2, 20, 30), np.uint16), imagej=True, resolution=(10, 10),
                     metadata={"axes": "TCYX", "unit": "um"})
    bad = tmp_path / "bad.tif"
    bad.write_bytes(b"II*\x00junk")
    app._add_paths([str(good), str(bad)])
    result = app._check_images()
    rows = {app.files.item(i, "text"): app.files.item(i, "values") for i in app.files.get_children()}
    assert rows["good.tif"][0] == "30x20" and str(rows["good.tif"][1]) == "2" and rows["good.tif"][-1] == "ok"
    assert rows["bad.tif"][-1] == "error"
    assert len(result.errors) == 1
    assert "not a readable TIFF" in app.issues.get("1.0", "end")

    # A real (non-dry) run with errors asks first; answering "no" starts nothing.
    app.output.set(str(tmp_path / "out"))
    with mock.patch.object(gui.messagebox, "askyesno", return_value=False) as ask:
        app._run()
    assert ask.called and app.worker is None
    assert (tmp_path / "out" / "precheck.txt").exists()
    assert not (tmp_path / "out" / "_trackbatch").exists()
