"""Tkinter GUI: pick images, edit parameters (or load them from a previous run), run the batch."""
from __future__ import annotations

import copy
import json
import os
import platform
import queue
import subprocess
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

from . import __version__
from . import params as P
from . import tools
from .batch import BatchConfig, Job, discover_inputs, run_batch
from .precheck import PrecheckResult, precheck
from .previous import load_previous, matlab_export_params

ENGINE_LABELS = {"trackmate": "TrackMate (Fiji)", "utrack": "u-track (MATLAB)"}


def _display(value) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, allow_nan=True)


def _is_branch(value) -> bool:
    if isinstance(value, dict):
        return bool(value)
    if isinstance(value, list):
        return bool(value) and all(isinstance(v, dict) for v in value)
    return False


class ParamTree(ttk.Frame):
    """Editable tree view of a nested parameter dict. Double-click a value to edit it."""

    def __init__(self, master, on_change=None):
        super().__init__(master)
        self.on_change = on_change
        self.params: dict = {}
        self.paths: dict[str, list] = {}
        self.tree = ttk.Treeview(self, columns=("value",), selectmode="browse")
        self.tree.heading("#0", text="Parameter")
        self.tree.heading("value", text="Value (double-click to edit)")
        self.tree.column("#0", width=320, stretch=True)
        self.tree.column("value", width=320, stretch=True)
        ysb = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=ysb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        ysb.grid(row=0, column=1, sticky="ns")
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        self.tree.bind("<Double-1>", self._begin_edit)
        self.tree.bind("<Return>", self._begin_edit)
        self._editor: ttk.Entry | None = None

    def load(self, params: dict) -> None:
        open_paths = {tuple(self.paths[i]) for i in self.paths if self.tree.exists(i) and self.tree.item(i, "open")}
        self.params = params
        self.tree.delete(*self.tree.get_children())
        self.paths.clear()
        self._insert("", params, [], open_paths)

    def _insert(self, parent, node, path, open_paths):
        items = node.items() if isinstance(node, dict) else enumerate(node)
        for key, value in items:
            p = path + [key]
            label = f"[{key}]" if isinstance(key, int) else str(key)
            if _is_branch(value):
                if isinstance(value, list):
                    first = value[0] if value else {}
                    hint = first.get("funcName") or first.get("feature") or ""
                    label += f"  {hint}" if hint else ""
                iid = self.tree.insert(parent, "end", text=label, values=("",),
                                       open=tuple(p) in open_paths or len(path) == 0)
                self.paths[iid] = p
                self._insert(iid, value, p, open_paths)
            else:
                iid = self.tree.insert(parent, "end", text=label, values=(_display(value),))
                self.paths[iid] = p

    def _get(self, path):
        node = self.params
        for k in path:
            node = node[k]
        return node

    def _set(self, path, value):
        node = self.params
        for k in path[:-1]:
            node = node[k]
        node[path[-1]] = value

    def _begin_edit(self, event=None):
        iid = self.tree.focus() if event is None or event.type != tk.EventType.ButtonPress else \
            self.tree.identify_row(event.y)
        if not iid or iid not in self.paths:
            return
        current = self._get(self.paths[iid])
        if _is_branch(current):
            return
        bbox = self.tree.bbox(iid, "value")
        if not bbox:
            return
        x, y, w, h = bbox
        self._end_edit(commit=False)
        var = tk.StringVar(value=_display(current))
        entry = ttk.Entry(self.tree, textvariable=var)
        entry.place(x=x, y=y, width=w, height=h)
        entry.focus_set()
        entry.select_range(0, "end")
        entry.bind("<Return>", lambda e: self._end_edit(True, iid, var))
        entry.bind("<Escape>", lambda e: self._end_edit(False))
        entry.bind("<FocusOut>", lambda e: self._end_edit(True, iid, var))
        self._editor = entry

    def _end_edit(self, commit, iid=None, var=None):
        editor, self._editor = self._editor, None
        if editor is None:
            return
        editor.destroy()
        if not commit or iid is None:
            return
        path = self.paths[iid]
        old = self._get(path)
        text = var.get()
        new = text if isinstance(old, str) else P.parse_value(text)
        if isinstance(old, (int, float)) and not isinstance(old, bool) and isinstance(new, str):
            messagebox.showerror("Invalid value", f"{path[-1]} expects a number, got {text!r}")
            return
        self._set(path, new)
        if _is_branch(new):
            self.load(self.params)
        else:
            self.tree.item(iid, values=(_display(new),))
        if self.on_change:
            self.on_change(path, new)


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"TrackBatch {__version__}")
        self.geometry("980x720")
        self.minsize(760, 560)
        self.settings = tools.load_app_settings()
        self.params = {e: P.defaults(e) for e in P.ENGINES}
        self.sources: dict[str, str | None] = {e: None for e in P.ENGINES}
        self.engine = tk.StringVar(value=self.settings.get("engine", "trackmate"))
        self.msgs: queue.Queue = queue.Queue()
        self.stop_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.job_rows: dict[int, str] = {}

        self._build()
        self._engine_changed()
        self.after(100, self._poll)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------ layout
    def _build(self):
        top = ttk.Frame(self, padding=(10, 8))
        top.pack(fill="x")
        ttk.Label(top, text="Engine:").pack(side="left")
        for key, label in ENGINE_LABELS.items():
            ttk.Radiobutton(top, text=label, value=key, variable=self.engine,
                            command=self._engine_changed).pack(side="left", padx=6)

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self.nb = nb
        nb.add(self._build_images(nb), text="1. Images")
        nb.add(self._build_params(nb), text="2. Parameters")
        nb.add(self._build_tools(nb), text="3. Tools")
        nb.add(self._build_run(nb), text="4. Run")

    def _build_images(self, parent):
        f = ttk.Frame(parent, padding=8)
        bar = ttk.Frame(f)
        bar.pack(fill="x")
        ttk.Button(bar, text="Add files…", command=self._add_files).pack(side="left")
        ttk.Button(bar, text="Add folder…", command=self._add_folder).pack(side="left", padx=4)
        ttk.Button(bar, text="Remove selected", command=self._remove_selected).pack(side="left", padx=4)
        ttk.Button(bar, text="Clear", command=self._clear_images).pack(side="left")
        ttk.Button(bar, text="Check images", command=self._check_images).pack(side="left", padx=(12, 0))
        self.recursive = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="Folders: include subfolders", variable=self.recursive).pack(side="left", padx=12)
        self.count_lbl = ttk.Label(bar, text="0 images")
        self.count_lbl.pack(side="right")
        box = ttk.Frame(f)
        box.pack(fill="both", expand=True, pady=(8, 0))
        cols = (("size", "Size", 80), ("c", "C", 36), ("z", "Z", 40), ("t", "Frames", 56),
                ("px", "Pixel size", 100), ("bits", "Bits", 48), ("check", "Check", 104))
        self.files = ttk.Treeview(box, columns=[c[0] for c in cols], selectmode="extended")
        self.files.heading("#0", text="Image")
        self.files.column("#0", width=300, stretch=True)
        for key, title, width in cols:
            self.files.heading(key, text=title)
            self.files.column(key, width=width, stretch=False, anchor="center")
        self.files.tag_configure("error", foreground="#b00020")
        self.files.tag_configure("warning", foreground="#a05a00")
        sb = ttk.Scrollbar(box, orient="vertical", command=self.files.yview)
        self.files.configure(yscrollcommand=sb.set)
        self.files.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.files.bind("<<TreeviewSelect>>", lambda e: self._show_image_issues())
        self.image_paths: dict[str, str] = {}
        self.precheck_result: PrecheckResult | None = None
        issues_box = ttk.Frame(f)
        issues_box.pack(fill="x", pady=(6, 0))
        self.issues = tk.Text(issues_box, height=7, wrap="word", state="disabled", font=("TkDefaultFont", 9))
        isb = ttk.Scrollbar(issues_box, orient="vertical", command=self.issues.yview)
        self.issues.configure(yscrollcommand=isb.set)
        self.issues.pack(side="left", fill="x", expand=True)
        isb.pack(side="right", fill="y")
        self.issues.tag_configure("error", foreground="#b00020")
        self.issues.tag_configure("warning", foreground="#a05a00")
        self._set_issues([("info", "Click 'Check images' to inspect sizes, channels, frames, pixel size and bit "
                                   "depth against the current parameters. The check also runs before each batch.")])
        return f

    def _build_params(self, parent):
        f = ttk.Frame(parent, padding=8)
        bar = ttk.Frame(f)
        bar.pack(fill="x")
        ttk.Button(bar, text="Reset to defaults", command=self._reset_params).pack(side="left")
        ttk.Button(bar, text="Load from previous run…", command=self._load_previous).pack(side="left", padx=4)
        ttk.Button(bar, text="Save parameters…", command=self._save_params).pack(side="left")

        self.tm_bar = ttk.Frame(f)
        ttk.Label(self.tm_bar, text="Detector:").pack(side="left")
        self.detector = tk.StringVar()
        cb = ttk.Combobox(self.tm_bar, textvariable=self.detector, values=list(P.TRACKMATE_DETECTORS), width=24)
        cb.pack(side="left", padx=4)
        cb.bind("<<ComboboxSelected>>", lambda e: self._switch_component("detector", "DETECTOR_NAME",
                                                                         self.detector.get(), P.TRACKMATE_DETECTORS))
        ttk.Label(self.tm_bar, text="Tracker:").pack(side="left", padx=(12, 0))
        self.tracker = tk.StringVar()
        cb = ttk.Combobox(self.tm_bar, textvariable=self.tracker, values=list(P.TRACKMATE_TRACKERS), width=28)
        cb.pack(side="left", padx=4)
        cb.bind("<<ComboboxSelected>>", lambda e: self._switch_component("tracker", "TRACKER_NAME",
                                                                         self.tracker.get(), P.TRACKMATE_TRACKERS))
        self.ut_bar = ttk.Frame(f)
        ttk.Label(self.ut_bar, text="Detection method:").pack(side="left")
        self.det_method = tk.StringVar()
        cb = ttk.Combobox(self.ut_bar, textvariable=self.det_method, state="readonly",
                          values=["subres_gaussian", "point_source"], width=20)
        cb.pack(side="left", padx=4)
        cb.bind("<<ComboboxSelected>>", lambda e: self._set_param(["detection", "method"], self.det_method.get()))

        self.source_lbl = ttk.Label(f, text="", foreground="#555")
        self.source_lbl.pack(fill="x", pady=(6, 2))
        self.tree = ParamTree(f, on_change=lambda path, v: self._sync_bars())
        self.tree.pack(fill="both", expand=True, pady=(4, 0))
        ttk.Label(f, foreground="#555", text="Values are JSON: numbers, true/false, null, \"text\", [1, 2], "
                  "{\"FEATURE\": 1.0}. Filters: [{\"feature\": \"QUALITY\", \"value\": 5, \"is_above\": true}]"
                  ).pack(fill="x", pady=(4, 0))
        return f

    def _build_tools(self, parent):
        f = ttk.Frame(parent, padding=12)
        self.fiji = tk.StringVar(value=self.settings.get("fiji_path", ""))
        self.matlab = tk.StringVar(value=self.settings.get("matlab_path", ""))
        self.utrack = tk.StringVar(value=self.settings.get("utrack_path", ""))
        rows = [
            ("Fiji (Fiji.app folder or launcher)", self.fiji, "any"),
            ("MATLAB executable", self.matlab, "file"),
            ("u-track 'software' folder", self.utrack, "dir"),
        ]
        for r, (label, var, kind) in enumerate(rows):
            ttk.Label(f, text=label).grid(row=r, column=0, sticky="w", pady=4)
            ttk.Entry(f, textvariable=var, width=70).grid(row=r, column=1, sticky="ew", padx=6)
            ttk.Button(f, text="Browse…", command=lambda v=var, k=kind: self._browse(v, k)).grid(row=r, column=2)
        f.columnconfigure(1, weight=1)
        ttk.Button(f, text="Check", command=self._check_tools).grid(row=3, column=1, sticky="w", pady=10)
        self.tools_lbl = ttk.Label(f, text="", justify="left")
        self.tools_lbl.grid(row=4, column=0, columnspan=3, sticky="w")
        ttk.Label(f, foreground="#555", justify="left", wraplength=820, text=(
            "TrackMate runs inside Fiji in headless mode (Fiji must be installed; TrackMate ships with it). "
            "u-track runs in MATLAB R2016b+ with the u-track 2.x code (github.com/DanuserLab/u-track); "
            "point the u-track field at its 'software' folder unless it is already on your MATLAB path.")
        ).grid(row=5, column=0, columnspan=3, sticky="w", pady=(12, 0))
        return f

    def _build_run(self, parent):
        f = ttk.Frame(parent, padding=8)
        opts = ttk.Frame(f)
        opts.pack(fill="x")
        ttk.Label(opts, text="Output folder:").grid(row=0, column=0, sticky="w")
        self.output = tk.StringVar(value=self.settings.get("output_dir", ""))
        ttk.Entry(opts, textvariable=self.output, width=70).grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Button(opts, text="Browse…", command=lambda: self._browse(self.output, "dir")).grid(row=0, column=2)
        opts.columnconfigure(1, weight=1)
        row2 = ttk.Frame(opts)
        row2.grid(row=1, column=0, columnspan=3, sticky="w", pady=6)
        ttk.Label(row2, text="Parallel workers:").pack(side="left")
        self.workers = tk.IntVar(value=int(self.settings.get("workers", 1)))
        ttk.Spinbox(row2, from_=1, to=32, textvariable=self.workers, width=4).pack(side="left", padx=(4, 16))
        self.skip = tk.BooleanVar(value=False)
        ttk.Checkbutton(row2, text="Skip images that already succeeded", variable=self.skip).pack(side="left")
        self.dry = tk.BooleanVar(value=False)
        ttk.Checkbutton(row2, text="Dry run (only write scripts)", variable=self.dry).pack(side="left", padx=12)

        btns = ttk.Frame(f)
        btns.pack(fill="x")
        self.run_btn = ttk.Button(btns, text="▶ Run batch", command=self._run)
        self.run_btn.pack(side="left")
        self.stop_btn = ttk.Button(btns, text="■ Stop", command=self._stop, state="disabled")
        self.stop_btn.pack(side="left", padx=4)
        ttk.Button(btns, text="Open output folder", command=self._open_output).pack(side="left", padx=4)
        self.progress = ttk.Progressbar(btns, mode="determinate", length=260)
        self.progress.pack(side="right")
        self.progress_lbl = ttk.Label(btns, text="")
        self.progress_lbl.pack(side="right", padx=8)

        pan = ttk.PanedWindow(f, orient="vertical")
        pan.pack(fill="both", expand=True, pady=(8, 0))
        self.jobs = ttk.Treeview(pan, columns=("status", "info"), height=6)
        self.jobs.heading("#0", text="Image")
        self.jobs.heading("status", text="Status")
        self.jobs.heading("info", text="Result / error")
        self.jobs.column("#0", width=300)
        self.jobs.column("status", width=80, stretch=False)
        self.jobs.column("info", width=420)
        pan.add(self.jobs, weight=1)
        logf = ttk.Frame(pan)
        self.log = tk.Text(logf, height=12, wrap="none", font=("Courier", 9))
        lsb = ttk.Scrollbar(logf, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=lsb.set, state="disabled")
        self.log.pack(side="left", fill="both", expand=True)
        lsb.pack(side="right", fill="y")
        pan.add(logf, weight=2)
        return f

    # ------------------------------------------------------------------ images
    def _image_list(self) -> list[str]:
        return [self.image_paths[i] for i in self.files.get_children()]

    def _count(self):
        self.count_lbl.configure(text=f"{len(self.image_paths)} images")

    def _add_paths(self, paths):
        existing = set(self.image_paths.values())
        for p in paths:
            if str(p) not in existing:
                iid = self.files.insert("", "end", text=Path(p).name, values=("",) * 7)
                self.image_paths[iid] = str(p)
                existing.add(str(p))
        self._count()

    def _clear_images(self):
        self.files.delete(*self.files.get_children())
        self.image_paths.clear()
        self._count()

    def _check_images(self, quiet=False) -> PrecheckResult | None:
        inputs = self._image_list()
        if not inputs:
            if not quiet:
                messagebox.showinfo("No images", "Add some images first.")
            return None
        e = self.engine.get()
        self.configure(cursor="watch")
        self.update_idletasks()
        try:
            result = precheck(e, self.params[e], inputs)
        finally:
            self.configure(cursor="")
        self.precheck_result = result
        by_path = {im.path: im for im in result.images}
        for iid in self.files.get_children():
            im = by_path[self.image_paths[iid]]
            status = result.status_of(im.path)
            if im.inspected:
                bits = "float" if im.is_float else str(im.bit_depth)
                vals = (im.size_text(), im.channels, im.slices, im.frames, im.pixel_text(), bits, status)
            else:
                vals = ("?", "?", "?", "?", "?", "?", status)
            self.files.item(iid, values=vals, tags=(status,) if status in ("error", "warning") else ())
        self._show_image_issues()
        return result

    def _set_issues(self, items):
        self.issues.configure(state="normal")
        self.issues.delete("1.0", "end")
        for level, text in items:
            self.issues.insert("end", text + "\n", (level,))
        self.issues.configure(state="disabled")

    def _show_image_issues(self):
        res = self.precheck_result
        if res is None:
            return
        selected = {self.image_paths[i] for i in self.files.selection()}
        shown = [i for i in res.issues if i.image is None or i.image in selected] if selected else res.issues
        summary = f"{len(res.errors)} error(s), {len(res.warnings)} warning(s)" + (
            " - select an image to see only its issues" if not selected else "")
        self._set_issues([("info", summary)] + [(i.level, str(i)) for i in shown])

    def _add_files(self):
        paths = filedialog.askopenfilenames(title="Select images", filetypes=[
            ("Images", "*.tif *.tiff *.nd2 *.czi *.lif *.lsm *.ims *.oib *.oir *.vsi *.stk"), ("All files", "*.*")])
        self._add_paths(paths)

    def _add_folder(self):
        folder = filedialog.askdirectory(title="Select folder with images")
        if not folder:
            return
        pattern = simpledialog.askstring("File pattern", "Glob pattern (blank = all common image types):",
                                         initialvalue="*.tif", parent=self)
        if pattern is None:
            return
        try:
            found = discover_inputs([folder], pattern.strip() or None, self.recursive.get())
        except OSError as exc:
            messagebox.showerror("Error", str(exc))
            return
        if not found:
            messagebox.showinfo("No images", "No matching images in that folder.")
        self._add_paths(found)

    def _remove_selected(self):
        for iid in self.files.selection():
            self.image_paths.pop(iid, None)
            self.files.delete(iid)
        self._count()

    # ------------------------------------------------------------------ params
    def _engine_changed(self):
        e = self.engine.get()
        if e == "trackmate":
            self.ut_bar.pack_forget()
            self.tm_bar.pack(fill="x", pady=(8, 0), after=self.tm_bar.master.winfo_children()[0])
        else:
            self.tm_bar.pack_forget()
            self.ut_bar.pack(fill="x", pady=(8, 0), after=self.ut_bar.master.winfo_children()[0])
        self._refresh_params()

    def _refresh_params(self):
        e = self.engine.get()
        self.tree.load(self.params[e])
        src = self.sources[e]
        self.source_lbl.configure(text=f"Parameters from: {src}" if src else "Parameters: defaults")
        self._sync_bars()

    def _sync_bars(self):
        p = self.params[self.engine.get()]
        if self.engine.get() == "trackmate":
            self.detector.set(p["detector"].get("DETECTOR_NAME", ""))
            self.tracker.set(p["tracker"].get("TRACKER_NAME", ""))
        else:
            self.det_method.set(p["detection"].get("method", "subres_gaussian"))

    def _set_param(self, path, value):
        P.set_path(self.params[self.engine.get()], ".".join(map(str, path)), value)
        self._refresh_params()

    def _switch_component(self, section, name_key, name, table):
        p = self.params["trackmate"]
        if p[section].get(name_key) == name:
            return
        new = {name_key: name}
        new.update(copy.deepcopy(table.get(name, {})))
        p[section] = new
        self._refresh_params()

    def _reset_params(self):
        e = self.engine.get()
        if messagebox.askyesno("Reset", f"Reset all {ENGINE_LABELS[e]} parameters to defaults?"):
            self.params[e] = P.defaults(e)
            self.sources[e] = None
            self._refresh_params()

    def _load_previous(self):
        path = filedialog.askopenfilename(title="Previous run (TrackMate .xml, u-track .mat, trackbatch .json)",
                                          filetypes=[("Previous runs", "*.xml *.mat *.json"), ("All files", "*.*")])
        if not path:
            return
        try:
            prev = load_previous(path, self.engine.get())
        except ValueError as exc:
            if path.lower().endswith(".mat") and messagebox.askyesno(
                    "Use MATLAB?", f"{exc}\n\nTry reading it with MATLAB now?"):
                try:
                    prev = matlab_export_params(path, self.matlab.get() or None, self.utrack.get() or None)
                except ValueError as exc2:
                    messagebox.showerror("Could not load", str(exc2))
                    return
            else:
                messagebox.showerror("Could not load", str(exc))
                return
        self.engine.set(prev.engine)
        self.params[prev.engine] = prev.params
        self.sources[prev.engine] = path
        self._engine_changed()
        existing = [p for p in prev.inputs if Path(p).is_file()]
        if existing and messagebox.askyesno(
                "Images", f"{chr(10).join(prev.notes)}\n\nAlso add the {len(existing)} image(s) from that run?"):
            self._add_paths(existing)
        elif prev.notes:
            messagebox.showinfo("Loaded", "\n".join(prev.notes))

    def _save_params(self):
        path = filedialog.asksaveasfilename(defaultextension=".json", filetypes=[("JSON", "*.json")],
                                            initialfile=f"{self.engine.get()}_params.json")
        if path:
            P.save_params(path, self.engine.get(), self.params[self.engine.get()])

    # ------------------------------------------------------------------ tools
    def _browse(self, var, kind):
        if kind == "dir":
            path = filedialog.askdirectory()
        elif kind == "file":
            path = filedialog.askopenfilename()
        else:
            path = filedialog.askopenfilename(title="Fiji launcher (or cancel to pick the Fiji.app folder)")
            if not path:
                path = filedialog.askdirectory(title="Fiji.app folder")
        if path:
            var.set(path)

    def _check_tools(self):
        fiji = tools.resolve_fiji(self.fiji.get() or None)
        matlab = tools.resolve_matlab(self.matlab.get() or None)
        ut = self.utrack.get()
        ut_ok = bool(ut) and Path(ut, "trackCloseGapsKalmanSparse.m").exists()
        self.tools_lbl.configure(text=(
            f"Fiji:    {fiji or 'NOT FOUND'}\n"
            f"MATLAB:  {matlab or 'NOT FOUND'}\n"
            f"u-track: {'OK' if ut_ok else ('not set (must already be on the MATLAB path)' if not ut else 'trackCloseGapsKalmanSparse.m not found in that folder')}"
        ))
        if fiji and not self.fiji.get():
            self.fiji.set(str(fiji))
        if matlab and not self.matlab.get():
            self.matlab.set(str(matlab))

    def _save_settings(self):
        self.settings.update({
            "fiji_path": self.fiji.get(), "matlab_path": self.matlab.get(), "utrack_path": self.utrack.get(),
            "output_dir": self.output.get(), "workers": self.workers.get(), "engine": self.engine.get(),
        })
        try:
            tools.save_app_settings(self.settings)
        except OSError:
            pass

    # ------------------------------------------------------------------ run
    def _run(self):
        if self.worker and self.worker.is_alive():
            return
        inputs = self._image_list()
        if not inputs:
            messagebox.showerror("No images", "Add some images on the Images tab first.")
            return
        if not self.output.get():
            messagebox.showerror("No output", "Choose an output folder.")
            return
        check = self._check_images(quiet=True)
        if check is not None:
            try:
                Path(self.output.get()).mkdir(parents=True, exist_ok=True)
                (Path(self.output.get()) / "precheck.txt").write_text(check.report() + "\n", encoding="utf-8")
            except OSError:
                pass
            if check.errors and not self.dry.get():
                bad = sorted({Path(i.image).name for i in check.errors if i.image})
                listing = "\n".join(f"  • {n}" for n in bad[:10]) + ("\n  …" if len(bad) > 10 else "")
                if not messagebox.askyesno(
                        "Pre-check found problems",
                        f"{len(check.errors)} error(s) in {len(bad) or 'the'} image(s):\n{listing}\n\n"
                        "These images will probably fail. Details are on the Images tab.\n\nRun anyway?"):
                    self.nb.select(0)
                    return
        self._save_settings()
        e = self.engine.get()
        cfg = BatchConfig(
            engine=e, params=copy.deepcopy(self.params[e]), inputs=inputs, output_dir=self.output.get(),
            fiji_path=self.fiji.get() or None, matlab_path=self.matlab.get() or None,
            utrack_path=self.utrack.get() or None, workers=max(1, int(self.workers.get())),
            dry_run=self.dry.get(), skip_existing=self.skip.get(), source=self.sources[e],
        )
        self.jobs.delete(*self.jobs.get_children())
        self.job_rows = {i: self.jobs.insert("", "end", text=Path(p).name, values=("pending", "")) for i, p in enumerate(inputs)}
        self.progress.configure(maximum=len(inputs), value=0)
        self.progress_lbl.configure(text=f"0/{len(inputs)}")
        self.stop_event.clear()
        self.run_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self._log(f"=== {ENGINE_LABELS[e]}: {len(inputs)} image(s) -> {cfg.output_dir}")
        if check is not None:
            for issue in check.issues:
                if issue.level != "info":
                    self._log(f"pre-check: {issue}")

        def work():
            try:
                run_batch(cfg, log=lambda m: self.msgs.put(("log", m)),
                          progress=lambda j: self.msgs.put(("job", copy.copy(j))), stop_event=self.stop_event)
            except Exception as exc:  # report anything to the user rather than dying silently
                self.msgs.put(("error", str(exc)))
            finally:
                self.msgs.put(("done", None))

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def _stop(self):
        self.stop_event.set()
        self._log("Stopping after the current output line…")

    def _poll(self):
        try:
            while True:
                kind, payload = self.msgs.get_nowait()
                if kind == "log":
                    self._log(payload)
                elif kind == "job":
                    self._update_job(payload)
                elif kind == "error":
                    self._log(f"ERROR: {payload}")
                    messagebox.showerror("Batch failed", payload)
                elif kind == "done":
                    self.run_btn.configure(state="normal")
                    self.stop_btn.configure(state="disabled")
                    self._refresh_job_statuses()
        except queue.Empty:
            pass
        self.after(100, self._poll)

    def _update_job(self, job: Job):
        iid = self.job_rows.get(job.id)
        if not iid:
            return
        info = job.error or ", ".join(f"{k}={v}" for k, v in job.stats.items())
        self.jobs.item(iid, values=(job.status, info))
        done = sum(self.jobs.set(i, "status") in ("ok", "failed", "skipped") for i in self.job_rows.values())
        self.progress.configure(value=done)
        self.progress_lbl.configure(text=f"{done}/{len(self.job_rows)}")

    def _refresh_job_statuses(self):
        # Final statuses come from the per-image status files written by the workers.
        from .batch import STATUS_FILE, build_jobs
        if not self.output.get() or self.dry.get():
            return
        cfg = BatchConfig(self.engine.get(), {}, self._image_list(), self.output.get())
        for job in build_jobs(cfg):
            sp = Path(job.output_dir) / STATUS_FILE
            if sp.exists() and job.id in self.job_rows:
                try:
                    data = json.loads(sp.read_text(encoding="utf-8"))
                except ValueError:
                    continue
                job.status, job.error, job.stats = data.get("status", ""), data.get("error", ""), data.get("stats", {})
                self._update_job(job)

    def _log(self, msg):
        self.log.configure(state="normal")
        self.log.insert("end", msg + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _open_output(self):
        out = self.output.get()
        if not out or not Path(out).exists():
            return
        if platform.system() == "Windows":
            os.startfile(out)  # type: ignore[attr-defined]
        elif platform.system() == "Darwin":
            subprocess.Popen(["open", out])
        else:
            subprocess.Popen(["xdg-open", out])

    def _on_close(self):
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno("Quit", "A batch is running. Stop it and quit?"):
                return
            self.stop_event.set()
        self._save_settings()
        self.destroy()


def main():
    App().mainloop()


if __name__ == "__main__":
    main()
