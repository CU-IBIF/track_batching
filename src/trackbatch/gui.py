"""Tkinter GUI: pick images, edit parameters (or load them from a previous run), run the batch."""
from __future__ import annotations

import copy
import json
import logging
import os
import platform
import queue
import subprocess
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

from . import __version__
from . import applog
from . import params as P
from . import tools
from .batch import BatchConfig, Job, discover_inputs, run_batch
from .precheck import ImageInfo, PrecheckResult, precheck
from .previous import load_previous, matlab_export_params

log = applog.logger

ENGINE_LABELS = {"trackmate": "TrackMate (Fiji)", "utrack": "u-track (MATLAB)"}
LEVEL_TAGS = {logging.DEBUG: "debug", logging.INFO: "info", logging.WARNING: "warning",
              logging.ERROR: "error", logging.CRITICAL: "error"}
MAX_LOG_LINES = 5000


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




def _fmt_duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def _enable_dnd(root) -> str | None:
    """Load the tkdnd extension; returns None on success or the reason it is unavailable."""
    try:
        from tkinterdnd2 import TkinterDnD
        TkinterDnD.require(root)
        return None
    except Exception as exc:  # missing package, unsupported platform, broken Tcl install...
        return f"{type(exc).__name__}: {exc}"


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        applog.setup_logging()
        self.title(f"TrackBatch {__version__}")
        self.geometry("1040x800")
        self.minsize(800, 600)
        self.settings = tools.load_app_settings()
        self.params = {e: P.defaults(e) for e in P.ENGINES}
        self.sources: dict[str, str | None] = {e: None for e in P.ENGINES}
        self.engine = tk.StringVar(value=self.settings.get("engine", "trackmate"))
        self.msgs: queue.Queue = queue.Queue()
        self.stop_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.job_rows: dict[int, str] = {}
        self.job_state: dict[int, str] = {}
        self.run_started: float | None = None
        self.run_dry = False
        self.current_image = ""
        self.log_entries: list[tuple[int, str, str]] = []

        # Route the app logger into the activity panel (thread-safe via the queue).
        self._log_handler = applog.CallbackHandler(lambda lvl, ts, msg: self.msgs.put(("logrec", (lvl, ts, msg))))
        log.addHandler(self._log_handler)
        threading.excepthook = self._thread_exception

        self._build()
        self.dnd_error = _enable_dnd(self)
        self._setup_drop_targets()
        self._engine_changed()
        self.after(100, self._poll)
        self.after(1000, self._tick)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        log.info("GUI started. Engine: %s", self.engine.get())

    # ------------------------------------------------------------------ errors
    def report_callback_exception(self, exc, val, tb):
        """Tk swallows exceptions from button handlers (and the windowed exe has no console)."""
        log.error("Unexpected error in the GUI", exc_info=(exc, val, tb))
        self._status(f"Error: {val}", "error")
        where = applog.current_log_file()
        messagebox.showerror("Unexpected error", f"{exc.__name__}: {val}\n\n"
                             f"The full details are in the activity log" + (f":\n{where}" if where else "."))

    def _thread_exception(self, args):
        log.error("Unexpected error in thread %s", args.thread.name if args.thread else "?",
                  exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    # ------------------------------------------------------------------ layout
    def _build(self):
        top = ttk.Frame(self, padding=(10, 8))
        top.pack(fill="x")
        ttk.Label(top, text="Engine:").pack(side="left")
        for key, label in ENGINE_LABELS.items():
            ttk.Radiobutton(top, text=label, value=key, variable=self.engine,
                            command=self._engine_changed).pack(side="left", padx=6)

        # Status bar (always visible): current activity, progress, elapsed / ETA.
        status = ttk.Frame(self, padding=(10, 4))
        status.pack(side="bottom", fill="x")
        self.status_lbl = ttk.Label(status, text="Ready", anchor="w")
        self.status_lbl.pack(side="left", fill="x", expand=True)
        self.time_lbl = ttk.Label(status, text="")
        self.time_lbl.pack(side="right", padx=(8, 0))
        self.progress = ttk.Progressbar(status, mode="determinate", length=240)
        self.progress.pack(side="right")
        self.progress_lbl = ttk.Label(status, text="")
        self.progress_lbl.pack(side="right", padx=8)

        pane = ttk.PanedWindow(self, orient="vertical")
        pane.pack(fill="both", expand=True, padx=10)
        nb = ttk.Notebook(pane)
        self.nb = nb
        nb.add(self._build_images(nb), text="1. Images")
        nb.add(self._build_params(nb), text="2. Parameters")
        nb.add(self._build_tools(nb), text="3. Tools")
        nb.add(self._build_run(nb), text="4. Run")
        pane.add(nb, weight=3)
        pane.add(self._build_activity(pane), weight=1)

    def _build_activity(self, parent):
        f = ttk.Frame(parent, padding=(0, 6, 0, 0))
        bar = ttk.Frame(f)
        bar.pack(fill="x")
        ttk.Label(bar, text="Activity log", font=("TkDefaultFont", 9, "bold")).pack(side="left")
        self.show_details = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="Show details", variable=self.show_details,
                        command=self._redraw_log).pack(side="left", padx=12)
        ttk.Button(bar, text="Open log folder", command=self._open_log_folder).pack(side="right")
        ttk.Button(bar, text="Open log file", command=self._open_log_file).pack(side="right", padx=4)
        ttk.Button(bar, text="Clear", command=self._clear_log).pack(side="right")
        ttk.Button(bar, text="Copy", command=self._copy_log).pack(side="right", padx=4)
        box = ttk.Frame(f)
        box.pack(fill="both", expand=True, pady=(4, 0))
        self.log_text = tk.Text(box, height=9, wrap="none", font=("Courier", 9), state="disabled")
        ysb = ttk.Scrollbar(box, orient="vertical", command=self.log_text.yview)
        xsb = ttk.Scrollbar(box, orient="horizontal", command=self.log_text.xview)
        self.log_text.configure(yscrollcommand=ysb.set, xscrollcommand=xsb.set)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        ysb.grid(row=0, column=1, sticky="ns")
        xsb.grid(row=1, column=0, sticky="ew")
        box.rowconfigure(0, weight=1)
        box.columnconfigure(0, weight=1)
        self.log_text.tag_configure("debug", foreground="#777")
        self.log_text.tag_configure("warning", foreground="#a05a00")
        self.log_text.tag_configure("error", foreground="#b00020")
        return f

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
        self.drop_hint = ttk.Label(f, foreground="#555", text="")
        self.drop_hint.pack(fill="x", pady=(6, 0))
        box = ttk.Frame(f)
        box.pack(fill="both", expand=True, pady=(4, 0))
        cols = (("size", "Width x Height", 110), ("c", "Channels", 70), ("z", "Z", 44), ("t", "Frames", 60),
                ("px", "Pixel size", 100), ("bits", "Bits", 48), ("check", "Check", 104))
        self.files = ttk.Treeview(box, columns=[c[0] for c in cols], selectmode="extended")
        self.files.heading("#0", text="Image")
        self.files.column("#0", width=260, stretch=True)
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
        self.files.bind("<Delete>", lambda e: self._remove_selected())
        self.image_paths: dict[str, str] = {}
        self.precheck_result: PrecheckResult | None = None
        issues_box = ttk.Frame(f)
        issues_box.pack(fill="x", pady=(6, 0))
        self.issues = tk.Text(issues_box, height=6, wrap="word", state="disabled", font=("TkDefaultFont", 9))
        isb = ttk.Scrollbar(issues_box, orient="vertical", command=self.issues.yview)
        self.issues.configure(yscrollcommand=isb.set)
        self.issues.pack(side="left", fill="x", expand=True)
        isb.pack(side="right", fill="y")
        self.issues.tag_configure("error", foreground="#b00020")
        self.issues.tag_configure("warning", foreground="#a05a00")
        self._set_issues([("info", "Image dimensions, channels, frames, pixel size and bit depth are read as soon as "
                                   "images are added, and checked against the parameters. The check runs again "
                                   "before each batch; 'Check images' re-runs it now.")])
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
        self.tree = ParamTree(f, on_change=self._param_edited)
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
        self.run_summary = ttk.Label(btns, text="")
        self.run_summary.pack(side="right")

        self.jobs = ttk.Treeview(f, columns=("status", "info"), height=8)
        self.jobs.heading("#0", text="Image")
        self.jobs.heading("status", text="Status")
        self.jobs.heading("info", text="Result / error")
        self.jobs.column("#0", width=300)
        self.jobs.column("status", width=80, stretch=False)
        self.jobs.column("info", width=460)
        self.jobs.tag_configure("failed", foreground="#b00020")
        self.jobs.tag_configure("running", foreground="#1a5fb4")
        self.jobs.pack(fill="both", expand=True, pady=(8, 0))
        return f

    # ------------------------------------------------------------------ activity log / status
    def _status(self, text, level="info"):
        colour = {"error": "#b00020", "warning": "#a05a00"}.get(level, "")
        self.status_lbl.configure(text=text, foreground=colour)

    def _append_log(self, level, ts, msg):
        self.log_entries.append((level, ts, msg))
        if len(self.log_entries) > MAX_LOG_LINES:
            del self.log_entries[: len(self.log_entries) - MAX_LOG_LINES]
        if level < logging.INFO and not self.show_details.get():
            return
        self._write_log_line(level, ts, msg)

    def _write_log_line(self, level, ts, msg):
        text = self.log_text
        at_end = text.yview()[1] > 0.999
        text.configure(state="normal")
        text.insert("end", f"{ts:%H:%M:%S} {msg}\n", (LEVEL_TAGS.get(level, "info"),))
        lines = int(text.index("end-1c").split(".")[0])
        if lines > MAX_LOG_LINES:
            text.delete("1.0", f"{lines - MAX_LOG_LINES}.0")
        text.configure(state="disabled")
        if at_end:
            text.see("end")

    def _redraw_log(self):
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")
        for level, ts, msg in self.log_entries:
            if level >= logging.INFO or self.show_details.get():
                self._write_log_line(level, ts, msg)

    def _clear_log(self):
        self.log_entries.clear()
        self._redraw_log()

    def _copy_log(self):
        self.clipboard_clear()
        self.clipboard_append(self.log_text.get("1.0", "end-1c"))
        self._status("Activity log copied to the clipboard")

    def _open_path(self, path):
        if not path or not Path(path).exists():
            messagebox.showinfo("Not found", f"{path or 'The log file'} does not exist (yet).")
            return
        log.debug("Opening %s", path)
        if platform.system() == "Windows":
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif platform.system() == "Darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])

    def _open_log_file(self):
        self._open_path(applog.current_log_file())

    def _open_log_folder(self):
        self._open_path(applog.log_dir())

    # ------------------------------------------------------------------ images
    def _setup_drop_targets(self):
        if self.dnd_error:
            log.warning("Drag and drop is not available (%s); use 'Add files…' / 'Add folder…'", self.dnd_error)
            self.drop_hint.configure(text="Add images with the buttons above.")
            return
        from tkinterdnd2 import DND_FILES

        for widget in (self.files, self.drop_hint, self.issues):
            widget.drop_target_register(DND_FILES)
            widget.dnd_bind("<<Drop>>", self._on_drop)
        self.drop_hint.configure(text="Tip: drag image files or folders from Explorer / Finder onto this list.")
        log.debug("Drag and drop enabled")

    def _on_drop(self, event):
        dropped = list(self.tk.splitlist(event.data))
        log.info("Dropped %d item(s)", len(dropped))
        self.handle_dropped_paths(dropped)
        return getattr(event, "action", "copy")

    def handle_dropped_paths(self, dropped: list[str]):
        files, folders = [], []
        for item in dropped:
            p = Path(item)
            if p.is_dir():
                folders.append(item)
            elif p.is_file():
                files.append(item)
            else:
                log.warning("Dropped item not found: %s", item)
        for folder in folders:
            try:
                found = discover_inputs([folder], None, self.recursive.get())
            except OSError as exc:
                log.error("Could not read folder %s: %s", folder, exc)
                continue
            log.info("Folder %s: %d image(s)%s", folder, len(found),
                     " (including subfolders)" if self.recursive.get() else "")
            files.extend(str(f) for f in found)
        if files:
            self._add_paths(files)
        elif dropped:
            self._status("No images found in the dropped items", "warning")

    def _image_list(self) -> list[str]:
        return [self.image_paths[i] for i in self.files.get_children()]

    def _count(self):
        self.count_lbl.configure(text=f"{len(self.image_paths)} images")

    def _add_paths(self, paths):
        existing = set(self.image_paths.values())
        added = 0
        for p in paths:
            if str(p) not in existing:
                iid = self.files.insert("", "end", text=Path(p).name, values=("…",) * 6 + ("",))
                self.image_paths[iid] = str(p)
                existing.add(str(p))
                added += 1
        self._count()
        skipped = len(list(paths)) - added
        log.info("Added %d image(s)%s; %d in the list", added,
                 f" ({skipped} already listed)" if skipped else "", len(self.image_paths))
        if added:
            self._check_images(quiet=True)

    def _clear_images(self):
        self.files.delete(*self.files.get_children())
        self.image_paths.clear()
        self.precheck_result = None
        self._count()
        log.info("Image list cleared")

    def _fill_row(self, iid, im: ImageInfo, status: str):
        if im.inspected:
            bits = "float" if im.is_float else str(im.bit_depth)
            vals = (im.size_text(), im.channels, im.slices, im.frames, im.pixel_text(), bits, status)
        else:
            vals = ("?", "?", "?", "?", "?", "?", status)
        self.files.item(iid, values=vals, tags=(status,) if status in ("error", "warning") else ())

    def _check_images(self, quiet=False) -> PrecheckResult | None:
        iids = list(self.files.get_children())
        inputs = [self.image_paths[i] for i in iids]
        if not inputs:
            if not quiet:
                messagebox.showinfo("No images", "Add some images first.")
            return None
        e = self.engine.get()
        log.info("Checking %d image(s) for %s…", len(inputs), ENGINE_LABELS[e])
        self.configure(cursor="watch")
        self.progress.configure(maximum=len(inputs), value=0)
        t0 = time.monotonic()

        def on_image(k, n, im: ImageInfo):
            # Results come back in input order, so match them by position, not by path text.
            self._fill_row(iids[k], im, "read" if im.inspected else ("not inspected" if im.readable else "error"))
            log.debug("%s: %s, C=%s Z=%s T=%s, %s, %s bit%s", im.name, im.size_text(), im.channels, im.slices,
                      im.frames, im.pixel_text(), "float" if im.is_float else im.bit_depth,
                      f" ({im.note})" if im.note else "")
            self.progress.configure(value=k + 1)
            self.progress_lbl.configure(text=f"{k + 1}/{n}")
            self._status(f"Reading image headers {k + 1}/{n}: {im.name}")
            self.update_idletasks()

        try:
            result = precheck(e, self.params[e], inputs, on_image=on_image)
        finally:
            self.configure(cursor="")
        self.precheck_result = result
        for iid, im in zip(iids, result.images):
            self._fill_row(iid, im, result.status_of(im.path))
        for issue in result.issues:
            level = {"error": logging.ERROR, "warning": logging.WARNING}.get(issue.level, logging.INFO)
            log.log(level, "Check: %s", issue)
        summary = (f"Checked {len(inputs)} image(s) in {time.monotonic() - t0:.1f} s: "
                   f"{len(result.errors)} error(s), {len(result.warnings)} warning(s)")
        log.info(summary)
        self._status(summary, "error" if result.errors else ("warning" if result.warnings else "info"))
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
        selected = {self.image_paths[i] for i in self.files.selection() if i in self.image_paths}
        shown = [i for i in res.issues if i.image is None or i.image in selected] if selected else res.issues
        summary = f"{len(res.errors)} error(s), {len(res.warnings)} warning(s)" + (
            " - select an image to see only its issues" if not selected else "")
        self._set_issues([("info", summary)] + [(i.level, str(i)) for i in shown])

    def _add_files(self):
        paths = filedialog.askopenfilenames(title="Select images", filetypes=[
            ("Images", "*.tif *.tiff *.nd2 *.czi *.lif *.lsm *.ims *.oib *.oir *.vsi *.stk"), ("All files", "*.*")])
        if paths:
            self._add_paths(list(paths))

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
            log.error("Could not read folder %s: %s", folder, exc)
            messagebox.showerror("Error", str(exc))
            return
        log.info("Folder %s with pattern %r: %d image(s)", folder, pattern.strip() or "(image types)", len(found))
        if not found:
            messagebox.showinfo("No images", "No matching images in that folder.")
            return
        self._add_paths([str(p) for p in found])

    def _remove_selected(self):
        sel = self.files.selection()
        for iid in sel:
            self.image_paths.pop(iid, None)
            self.files.delete(iid)
        self._count()
        if sel:
            log.info("Removed %d image(s)", len(sel))

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
        if getattr(self, "_last_engine", None) not in (None, e):
            log.info("Engine changed to %s", ENGINE_LABELS[e])
            if self.image_paths:
                self._check_images(quiet=True)  # the checks differ per engine
        self._last_engine = e

    def _refresh_params(self):
        e = self.engine.get()
        self.tree.load(self.params[e])
        src = self.sources[e]
        self.source_lbl.configure(text=f"Parameters from: {src}" if src else "Parameters: defaults")
        self._sync_bars()

    def _param_edited(self, path, value):
        log.info("Parameter %s = %s", ".".join(map(str, path)), _display(value))
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
        log.info("Parameter %s = %s", ".".join(map(str, path)), _display(value))
        self._refresh_params()

    def _switch_component(self, section, name_key, name, table):
        p = self.params["trackmate"]
        if p[section].get(name_key) == name:
            return
        new = {name_key: name}
        new.update(copy.deepcopy(table.get(name, {})))
        p[section] = new
        log.info("TrackMate %s set to %s (its settings were reset to defaults)", section, name)
        self._refresh_params()

    def _reset_params(self):
        e = self.engine.get()
        if messagebox.askyesno("Reset", f"Reset all {ENGINE_LABELS[e]} parameters to defaults?"):
            self.params[e] = P.defaults(e)
            self.sources[e] = None
            log.info("%s parameters reset to defaults", ENGINE_LABELS[e])
            self._refresh_params()

    def _load_previous(self):
        path = filedialog.askopenfilename(title="Previous run (TrackMate .xml, u-track .mat, trackbatch .json)",
                                          filetypes=[("Previous runs", "*.xml *.mat *.json"), ("All files", "*.*")])
        if not path:
            return
        log.info("Loading parameters from %s", path)
        try:
            prev = load_previous(path, self.engine.get())
        except ValueError as exc:
            log.warning("Could not load %s: %s", path, exc)
            if path.lower().endswith(".mat") and messagebox.askyesno(
                    "Use MATLAB?", f"{exc}\n\nTry reading it with MATLAB now?"):
                try:
                    log.info("Exporting parameters with MATLAB…")
                    prev = matlab_export_params(path, self.matlab.get() or None, self.utrack.get() or None)
                except ValueError as exc2:
                    log.error("MATLAB export failed: %s", exc2)
                    messagebox.showerror("Could not load", str(exc2))
                    return
            else:
                messagebox.showerror("Could not load", str(exc))
                return
        for note in prev.notes:
            log.info(note)
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
            log.info("Parameters saved to %s", path)

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
        ut_text = "OK" if ut_ok else ("not set (must already be on the MATLAB path)" if not ut
                                      else "trackCloseGapsKalmanSparse.m not found in that folder")
        self.tools_lbl.configure(text=(
            f"Fiji:    {fiji or 'NOT FOUND'}\n"
            f"MATLAB:  {matlab or 'NOT FOUND'}\n"
            f"u-track: {ut_text}"
        ))
        log.info("Tools: Fiji=%s | MATLAB=%s | u-track=%s", fiji or "NOT FOUND", matlab or "NOT FOUND", ut_text)
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
        except OSError as exc:
            log.warning("Could not save settings to %s: %s", tools.CONFIG_FILE, exc)

    # ------------------------------------------------------------------ run
    def _cannot_run(self, title, message):
        log.warning("Run batch: %s", message)
        self._status(message, "warning")
        messagebox.showerror(title, message)

    def _run(self):
        log.info("'Run batch' clicked")
        if self.worker and self.worker.is_alive():
            log.warning("Run batch: a batch is already running")
            return
        inputs = self._image_list()
        if not inputs:
            self._cannot_run("No images", "Add some images on the Images tab first.")
            return
        if not self.output.get():
            self._cannot_run("No output", "Choose an output folder on the Run tab.")
            return
        try:
            workers = max(1, int(self.workers.get()))
        except (tk.TclError, ValueError):
            self._cannot_run("Workers", "The number of parallel workers must be a whole number.")
            return
        check = self._check_images(quiet=True)
        if check is not None:
            try:
                Path(self.output.get()).mkdir(parents=True, exist_ok=True)
                (Path(self.output.get()) / "precheck.txt").write_text(check.report() + "\n", encoding="utf-8")
            except OSError as exc:
                self._cannot_run("Output folder", f"Cannot write to the output folder {self.output.get()}: {exc}")
                return
            if check.errors and not self.dry.get():
                bad = sorted({Path(i.image).name for i in check.errors if i.image})
                listing = "\n".join(f"  • {n}" for n in bad[:10]) + ("\n  …" if len(bad) > 10 else "")
                if not messagebox.askyesno(
                        "Pre-check found problems",
                        f"{len(check.errors)} error(s) in {len(bad) or 'the'} image(s):\n{listing}\n\n"
                        "These images will probably fail. Details are on the Images tab.\n\nRun anyway?"):
                    log.info("Run cancelled after the pre-check")
                    self._status("Run cancelled - see the pre-check results on the Images tab", "warning")
                    self.nb.select(0)
                    return
                log.warning("Running despite %d pre-check error(s)", len(check.errors))
        self._save_settings()
        e = self.engine.get()
        cfg = BatchConfig(
            engine=e, params=copy.deepcopy(self.params[e]), inputs=inputs, output_dir=self.output.get(),
            fiji_path=self.fiji.get() or None, matlab_path=self.matlab.get() or None,
            utrack_path=self.utrack.get() or None, workers=workers,
            dry_run=self.dry.get(), skip_existing=self.skip.get(), source=self.sources[e],
        )
        self.jobs.delete(*self.jobs.get_children())
        self.job_rows = {i: self.jobs.insert("", "end", text=Path(p).name, values=("pending", ""))
                         for i, p in enumerate(inputs)}
        self.job_state = {i: "pending" for i in range(len(inputs))}
        self.progress.configure(maximum=len(inputs), value=0)
        self.progress_lbl.configure(text=f"0/{len(inputs)}")
        self.stop_event.clear()
        self.run_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.run_started = time.monotonic()
        self.run_dry = cfg.dry_run
        self.current_image = ""
        self.nb.select(3)
        log.info("Starting %s%s: %d image(s), %d worker(s), output %s", ENGINE_LABELS[e],
                 " (dry run)" if cfg.dry_run else "", len(inputs), workers, cfg.output_dir)
        self._status("Starting batch…")

        def work():
            try:
                run_batch(cfg, log=log.info, progress=lambda j: self.msgs.put(("job", copy.copy(j))),
                          stop_event=self.stop_event)
            except Exception as exc:  # report anything to the user rather than dying silently
                log.error("Batch failed: %s", exc, exc_info=not isinstance(exc, RuntimeError))
                self.msgs.put(("error", str(exc)))
            finally:
                self.msgs.put(("done", None))

        self.worker = threading.Thread(target=work, name="batch", daemon=True)
        self.worker.start()

    def _stop(self):
        self.stop_event.set()
        log.warning("Stop requested - the running Fiji/MATLAB process will be terminated")
        self._status("Stopping…", "warning")

    def _poll(self):
        try:
            while True:
                kind, payload = self.msgs.get_nowait()
                if kind == "logrec":
                    self._append_log(*payload)
                elif kind == "job":
                    self._update_job(payload)
                elif kind == "error":
                    self._status(f"Batch failed: {payload.splitlines()[0]}", "error")
                    messagebox.showerror("Batch failed", payload)
                elif kind == "done":
                    self._batch_finished()
        except queue.Empty:
            pass
        self.after(100, self._poll)

    def _tick(self):
        if self.worker and self.worker.is_alive() and self.run_started is not None:
            self._show_run_progress()
        self.after(1000, self._tick)

    def _run_counts(self):
        states = list(self.job_state.values())
        done = sum(s in ("ok", "failed", "skipped") for s in states)
        return done, states.count("failed"), states.count("running"), len(states)

    def _show_run_progress(self):
        done, failed, running, total = self._run_counts()
        elapsed = time.monotonic() - (self.run_started or time.monotonic())
        timing = f"elapsed {_fmt_duration(elapsed)}"
        processed = sum(s in ("ok", "failed") for s in self.job_state.values())  # skipped ones take no time
        if processed and done < total:
            timing += f" · ETA ~{_fmt_duration(elapsed / processed * (total - done))}"
        self.time_lbl.configure(text=timing)
        self.progress.configure(maximum=max(1, total), value=done)
        self.progress_lbl.configure(text=f"{done}/{total}")
        text = f"Running: {done}/{total} done"
        if failed:
            text += f", {failed} failed"
        if running:
            text += f" · now: {self.current_image}" if self.current_image else f" · {running} running"
        elif done == 0:
            text += " · waiting for Fiji/MATLAB to start (this can take a minute)"
        self._status(text, "warning" if failed else "info")
        self.run_summary.configure(text=text.replace("Running: ", ""))

    def _update_job(self, job: Job):
        iid = self.job_rows.get(job.id)
        if not iid:
            return
        previous = self.job_state.get(job.id)
        self.job_state[job.id] = job.status
        info = job.error or ", ".join(f"{k}={v}" for k, v in job.stats.items())
        self.jobs.item(iid, values=(job.status, info), tags=(job.status,))
        name = Path(job.image).name
        if job.status == "running":
            self.current_image = name
            self.jobs.see(iid)
        if job.status != previous:
            if job.status == "failed":
                log.error("%s failed: %s", name, job.error)
            elif job.status == "ok":
                log.info("%s done (%s)", name, info)
        self._show_run_progress()

    def _batch_finished(self):
        self.run_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self._refresh_job_statuses()
        done, failed, _, total = self._run_counts()
        ok = sum(s == "ok" for s in self.job_state.values())
        skipped = sum(s == "skipped" for s in self.job_state.values())
        elapsed = _fmt_duration(time.monotonic() - (self.run_started or time.monotonic()))
        self.progress.configure(value=done)
        self.time_lbl.configure(text=f"took {elapsed}")
        if self.run_dry:
            for iid in self.job_rows.values():
                self.jobs.item(iid, values=("dry run", "scripts written, not run"))
            msg = f"Dry run finished in {elapsed}: scripts and manifests written to the output folder"
            log.info(msg)
            self._status(msg)
            self.run_started = None
            return
        not_done = total - done
        msg = f"Batch finished in {elapsed}: {ok} ok, {failed} failed" + (f", {skipped} skipped" if skipped else "") \
            + (f", {not_done} not processed" if not_done else "")
        level = "error" if failed or not_done else "info"
        (log.warning if level == "error" else log.info)(msg)
        self._status(msg, level)
        self.run_summary.configure(text=msg.split(": ", 1)[1])
        self.run_started = None
        if self.stop_event.is_set():
            return
        if failed or not_done:
            messagebox.showwarning("Batch finished with problems",
                                   f"{msg}.\n\nThe Run tab lists the error for each image, and the activity "
                                   "log (bottom) shows the Fiji/MATLAB output. It is also saved in "
                                   "<output>/_trackbatch/batch.log.")

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
        log.info(msg)

    def _open_output(self):
        out = self.output.get()
        if not out or not Path(out).exists():
            messagebox.showinfo("Output", "The output folder does not exist yet.")
            return
        self._open_path(out)

    def _on_close(self):
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno("Quit", "A batch is running. Stop it and quit?"):
                return
            self.stop_event.set()
        self._save_settings()
        log.info("GUI closed")
        self.destroy()

    def destroy(self):
        log.removeHandler(self._log_handler)
        super().destroy()


def selftest(report_path: str) -> int:
    """Smoke test for the frozen app: open the GUI, add a TIFF, check it was read. Writes a JSON report."""
    import tempfile

    import numpy as np
    import tifffile

    report: dict = {"version": __version__}
    try:
        app = App()
        app.update()
        report["dnd"] = app.dnd_error or "ok"
        report["log_file"] = str(applog.current_log_file())
        with tempfile.TemporaryDirectory() as tmp:
            img = Path(tmp) / "selftest.tif"
            tifffile.imwrite(img, np.zeros((3, 2, 20, 30), np.uint16), imagej=True, resolution=(10, 10),
                             metadata={"axes": "TCYX", "unit": "um"})
            app._add_paths([img.as_posix()])  # forward slashes, like the Windows file dialog
            app.update()
            report["row"] = [str(v) for v in app.files.item(app.files.get_children()[0], "values")]
        app.destroy()
        report["ok"] = report["dnd"] == "ok" and report["row"][:4] == ["30x20", "2", "1", "3"]
    except Exception as exc:
        log.exception("Self-test failed")
        report["ok"] = False
        report["error"] = f"{type(exc).__name__}: {exc}"
    Path(report_path).write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 0 if report["ok"] else 1


def main():
    applog.setup_logging()
    try:
        App().mainloop()
    except Exception:
        log.critical("TrackBatch crashed", exc_info=True)
        raise


if __name__ == "__main__":
    main()
