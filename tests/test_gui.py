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


@pytest.fixture(scope="session")
def _session_app(tmp_path_factory):
    # One Tk window for the whole session: on Windows, creating many Tk interpreters in one
    # process intermittently fails ("Can't find a usable init.tcl ... No error", then
    # "invalid command name tcl_findLibrary"). The real app only ever creates one.
    cfg = tmp_path_factory.mktemp("cfg")
    with mock.patch.object(gui.tools, "CONFIG_DIR", cfg), \
         mock.patch.object(gui.tools, "CONFIG_FILE", cfg / "settings.json"):
        a = gui.App()
        a.update()
        yield a
        a.destroy()


@pytest.fixture
def app(_session_app):
    a = _session_app
    # Reset everything a test may have changed.
    if a.worker is not None:
        a.worker.join(timeout=30)
    a.worker = None
    a._clear_images()
    a.params = {e: P.defaults(e) for e in P.ENGINES}
    a.sources = {e: None for e in P.ENGINES}
    a.engine.set("trackmate")
    a._engine_changed()
    a.output.set("")
    a.dry.set(False)
    a.skip.set(False)
    a.jobs.delete(*a.jobs.get_children())
    a.job_rows, a.job_state = {}, {}
    a.stop_event.clear()
    a.update()
    a.log_entries.clear()
    a._redraw_log()
    a.nb.select(0)
    a.update()
    yield a


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


def _tiff(path, shape=(4, 2, 20, 30), axes="TCYX"):
    import numpy as np
    import tifffile

    tifffile.imwrite(path, np.zeros(shape, np.uint16), imagej=True, resolution=(10, 10),
                     metadata={"axes": axes, "unit": "um"})
    return path


def _log_text(app):
    return "\n".join(msg for _, _, msg in app.log_entries)


def _pump(app, seconds=0.3):
    t0 = time.time()
    while time.time() - t0 < seconds:
        app.update()
        time.sleep(0.02)


def test_dimensions_shown_on_add_even_with_unnormalised_paths(app, tmp_path):
    # Windows file dialogs return "C:/x/y.tif" while Path() gives "C:\\x\\y.tif"; "dir//y.tif" vs
    # "dir/y.tif" reproduces the same mismatch on every OS. The table must still be filled in.
    _tiff(tmp_path / "movie.tif")
    odd = str(tmp_path) + "//movie.tif"
    app._add_paths([odd])  # no explicit "Check images" click
    row = app.files.item(app.files.get_children()[0], "values")
    assert row[0] == "30x20" and str(row[1]) == "2" and str(row[3]) == "4" and row[-1] == "ok"
    assert app._image_list() == [odd]


def test_drop_files_and_folders(app, tmp_path):
    folder = tmp_path / "drop"
    folder.mkdir()
    _tiff(folder / "a.tif")
    _tiff(folder / "b.tif", (3, 16, 16), "TYX")
    (folder / "notes.txt").write_text("x")
    single = _tiff(tmp_path / "single.tif")
    app.handle_dropped_paths([str(folder), str(single), str(tmp_path / "missing.tif")])
    names = sorted(app.files.item(i, "text") for i in app.files.get_children())
    assert names == ["a.tif", "b.tif", "single.tif"]
    _pump(app)
    text = _log_text(app)
    assert "Dropped item not found" in text and "Added 3 image(s)" in text


def test_drag_and_drop_is_enabled(app):
    pytest.importorskip("tkinterdnd2")
    assert app.dnd_error is None


def test_gui_errors_are_logged_not_swallowed(app):
    with mock.patch.object(gui.messagebox, "showerror") as shown:
        try:
            raise KeyError("C:\\data\\x.tif")
        except KeyError:
            import sys
            app.report_callback_exception(*sys.exc_info())
    assert shown.called
    _pump(app)
    assert "Unexpected error in the GUI" in _log_text(app)
    log_file = gui.applog.current_log_file()
    assert log_file and "Traceback" in log_file.read_text(encoding="utf-8")


def test_run_problems_are_reported(app, tmp_path):
    with mock.patch.object(gui.messagebox, "showerror") as shown:
        app._run()  # no images
    assert shown.called
    _tiff(tmp_path / "m.tif")
    app._add_paths([str(tmp_path / "m.tif")])
    app.output.set("")
    with mock.patch.object(gui.messagebox, "showerror"):
        app._run()
    _pump(app)
    text = _log_text(app)
    assert "'Run batch' clicked" in text and "Add some images" in text and "Choose an output folder" in text
    assert "output folder" in app.status_lbl.cget("text")


def test_dry_run_progress_and_log(app, tmp_path):
    _tiff(tmp_path / "m1.tif")
    _tiff(tmp_path / "m2.tif")
    app._add_paths([str(tmp_path / "m1.tif"), str(tmp_path / "m2.tif")])
    app.output.set(str(tmp_path / "out"))
    app.dry.set(True)
    app._run()
    t0 = time.time()
    while app.worker.is_alive() and time.time() - t0 < 20:
        app.update()
        time.sleep(0.02)
    _pump(app)
    text = _log_text(app)
    assert "Starting TrackMate (Fiji) (dry run): 2 image(s)" in text
    assert "[dry-run] worker 0" in text and "Dry run finished" in text
    assert "Dry run finished" in app.status_lbl.cget("text")
    assert (tmp_path / "out" / "_trackbatch" / "batch.log").exists()
