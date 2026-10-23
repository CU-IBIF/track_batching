"""Inspect images before a batch and flag problems with the chosen parameters.

Only file headers are read (no pixel data), so checking hundreds of movies is
quick. TIFF variants (ImageJ hyperstacks, OME-TIFF, plain multi-page) are
inspected with tifffile; other formats (.nd2, .czi, ...) are opened by
Bio-Formats inside Fiji/MATLAB and are listed as "not inspected".
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

TIFF_SUFFIXES = (".tif", ".tiff")
# Units that mean "not calibrated"
_NO_UNIT = {"", "pixel", "pixels", "px"}


@dataclass
class ImageInfo:
    path: str
    readable: bool = False
    inspected: bool = False
    width: int | None = None
    height: int | None = None
    channels: int = 1
    slices: int = 1
    frames: int = 1
    bit_depth: int | None = None
    is_float: bool = False
    pixel_size: float | None = None  # None = uncalibrated
    unit: str = ""
    frame_interval: float | None = None
    note: str = ""

    @property
    def name(self) -> str:
        return Path(self.path).name

    def size_text(self) -> str:
        return f"{self.width}x{self.height}" if self.width else "?"

    def pixel_text(self) -> str:
        if not self.inspected:
            return "?"
        return f"{self.pixel_size:.4g} {self.unit}" if self.pixel_size else "uncalibrated"


@dataclass
class Issue:
    level: str  # "error" | "warning" | "info"
    message: str
    image: str | None = None  # None = applies to the whole batch

    def __str__(self) -> str:
        where = f"{Path(self.image).name}: " if self.image else ""
        return f"{self.level.upper():7} {where}{self.message}"


@dataclass
class PrecheckResult:
    images: list[ImageInfo]
    issues: list[Issue] = field(default_factory=list)

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.level == "error"]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.level == "warning"]

    def status_of(self, path: str) -> str:
        levels = {i.level for i in self.issues if i.image == path}
        for level in ("error", "warning"):
            if level in levels:
                return level
        info = next((im for im in self.images if im.path == path), None)
        return "ok" if info and info.inspected else "not inspected"

    def table(self) -> str:
        rows = [("Image", "Size", "C", "Z", "T", "Pixel size", "Bits", "Check")]
        for im in self.images:
            bits = ("float" if im.is_float else str(im.bit_depth)) if im.inspected else "?"
            dims = (str(im.channels), str(im.slices), str(im.frames)) if im.inspected else ("?", "?", "?")
            rows.append((im.name, im.size_text(), *dims, im.pixel_text(), bits, self.status_of(im.path)))
        widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
        return "\n".join("  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip() for r in rows)

    def report(self) -> str:
        lines = [self.table(), ""]
        if self.issues:
            lines += [str(i) for i in self.issues]
        else:
            lines.append("No problems found.")
        lines.append(f"\n{len(self.errors)} error(s), {len(self.warnings)} warning(s)")
        return "\n".join(lines)


# --------------------------------------------------------------------------
# Reading headers
# --------------------------------------------------------------------------

def _ome_info(info: ImageInfo, ome_xml: str) -> None:
    root = ET.fromstring(ome_xml)
    pixels = next((el for el in root.iter() if el.tag.endswith("Pixels")), None)
    if pixels is None:
        return
    a = pixels.attrib
    info.width, info.height = int(a.get("SizeX", 0)) or None, int(a.get("SizeY", 0)) or None
    info.channels = int(a.get("SizeC", 1))
    info.slices = int(a.get("SizeZ", 1))
    info.frames = int(a.get("SizeT", 1))
    if a.get("PhysicalSizeX"):
        info.pixel_size = float(a["PhysicalSizeX"])
        info.unit = a.get("PhysicalSizeXUnit", "µm")
    if a.get("TimeIncrement"):
        info.frame_interval = float(a["TimeIncrement"])


def inspect_image(path: str | Path) -> ImageInfo:
    path = Path(path)
    info = ImageInfo(str(path))
    if not path.name.lower().endswith(TIFF_SUFFIXES):
        info.readable = path.is_file()
        info.note = "not inspected (opened with Bio-Formats at run time)"
        return info
    import logging

    import tifffile

    logging.getLogger("tifffile").setLevel(logging.CRITICAL)  # we report problems ourselves
    try:
        with tifffile.TiffFile(path) as tf:
            page = tf.pages[0]
            series = tf.series[0]
            info.width, info.height = int(page.imagewidth), int(page.imagelength)
            info.is_float = page.dtype is not None and page.dtype.kind == "f"
            info.bit_depth = int(page.bitspersample)
            ij = tf.imagej_metadata or {}
            if tf.is_ome and tf.ome_metadata:
                _ome_info(info, tf.ome_metadata)
            elif ij:
                info.channels = int(ij.get("channels", 1))
                info.slices = int(ij.get("slices", 1))
                n_pages = len(tf.pages)
                info.frames = int(ij.get("frames", max(1, n_pages // max(1, info.channels * info.slices))))
                if ij.get("finterval"):
                    info.frame_interval = float(ij["finterval"])
            else:
                # Plain multi-page TIFF: ImageJ and the u-track script treat the pages as one axis.
                axes, shape = series.axes, series.shape
                dims = dict(zip(axes, shape))
                info.channels = int(dims.get("C", 1))
                info.slices = int(dims.get("Z", 1))
                info.frames = int(dims.get("T", 1))
                other = 1
                for ax, n in dims.items():
                    if ax not in "TZCYXS":
                        other *= int(n)
                info.frames *= other
            if info.pixel_size is None:
                unit = str(ij.get("unit", "")).strip()
                xres = page.tags.get("XResolution")
                if xres is not None and unit.lower() not in _NO_UNIT:
                    num, den = xres.value
                    if num:
                        info.pixel_size = den / num
                        info.unit = "µm" if unit in ("micron", "\\u00B5m", "um") else unit
            info.readable = info.inspected = True
    except Exception as exc:  # corrupt / truncated / not really a TIFF
        info.note = f"not a readable TIFF ({type(exc).__name__}: {exc})"
    return info


# --------------------------------------------------------------------------
# Checks
# --------------------------------------------------------------------------

def _close(a: float, b: float, rel: float = 1e-3) -> bool:
    return abs(a - b) <= rel * max(abs(a), abs(b))


def precheck(engine: str, params: dict, inputs: list[str]) -> PrecheckResult:
    images = [inspect_image(p) for p in inputs]
    res = PrecheckResult(images)
    add = res.issues.append

    if engine == "trackmate":
        channel = int(params.get("detector", {}).get("TARGET_CHANNEL") or 1)
        swap = params.get("image", {}).get("swap_z_t", "auto")
        cal = params.get("calibration") or {}
        cal_override = bool(cal.get("pixel_width"))
    else:
        img = params.get("image", {})
        channel = int(img.get("channel") or 1)
        first = int(img.get("first_frame") or 1)
        swap = "auto"
        cal_override = False
        bit_param = params.get("detection", {}).get("detectionParam", {}).get("bitDepth")
        method = params.get("detection", {}).get("method")

    for im in images:
        if not im.readable:
            add(Issue("error", im.note or "file not found", im.path))
            continue
        if not im.inspected:
            add(Issue("info", im.note, im.path))
            continue
        z, t = im.slices, im.frames
        if z > 1 and t == 1 and swap in ("auto", True):
            z, t = 1, im.slices
            add(Issue("info", f"{im.slices} Z-slices and 1 frame: will be treated as {t} time points", im.path))
        if channel > im.channels:
            add(Issue("error", f"channel {channel} requested but the image has {im.channels}", im.path))
        if t == 1:
            add(Issue("warning", "only one time point - there is nothing to track", im.path))
        if engine == "utrack":
            if z > 1:
                add(Issue("error", f"3D movie ({z} slices): the u-track batch is 2D only", im.path))
            if first > t:
                add(Issue("error", f"first_frame={first} but the movie has {t} frame(s)", im.path))
            if im.is_float:
                add(Issue("warning", "floating-point pixels are converted to uint16 for u-track; "
                                     "values below 1 are lost - rescale first", im.path))
            elif method == "subres_gaussian" and bit_param and im.bit_depth and im.bit_depth < int(bit_param):
                add(Issue("warning", f"{im.bit_depth}-bit image but detectionParam.bitDepth={bit_param}", im.path))
        elif im.pixel_size is None and not cal_override:
            add(Issue("warning", "no pixel calibration - TrackMate distances (RADIUS, LINKING_MAX_DISTANCE) "
                                 "will be in pixels", im.path))

    # ---- consistency across the batch ------------------------------------
    seen = [im for im in images if im.inspected]
    if len(seen) > 1:
        sizes = {im.size_text() for im in seen}
        if len(sizes) > 1:
            add(Issue("info", f"image sizes differ ({', '.join(sorted(sizes))}); this is fine - each image "
                              "is analysed in full"))
        cals = [(im.pixel_size, im.unit) for im in seen if im.pixel_size]
        if cals and any(not (_close(p, cals[0][0]) and u == cals[0][1]) for p, u in cals):
            values = ", ".join(sorted({f"{p:.4g} {u}" for p, u in cals}))
            if engine == "utrack":
                add(Issue("warning", f"pixel sizes differ ({values}); u-track parameters are in pixels, so "
                                     "search radii and psfSigma mean different physical distances - "
                                     "consider separate batches"))
            elif cal_override:
                add(Issue("warning", f"pixel sizes differ ({values}) but the calibration override applies "
                                     "one pixel size to every image"))
            else:
                add(Issue("info", f"pixel sizes differ ({values}); TrackMate works in physical units, "
                                  "so shared parameters stay comparable"))
        if engine == "trackmate" and cal_override and cals:
            add(Issue("info", f"calibration override pixel_width={cal['pixel_width']} replaces the "
                              "calibration stored in the files"))
        depths = {("float" if im.is_float else im.bit_depth) for im in seen}
        if len(depths) > 1:
            add(Issue("warning", f"bit depths differ ({', '.join(map(str, sorted(depths, key=str)))}); "
                                 "intensity thresholds and u-track bitDepth apply to all images alike"))
        chans = {im.channels for im in seen}
        if len(chans) > 1:
            add(Issue("warning", f"channel counts differ ({', '.join(map(str, sorted(chans)))}); "
                                 f"channel {channel} is analysed in every image"))
        intervals = {round(im.frame_interval, 6) for im in seen if im.frame_interval}
        if len(intervals) > 1:
            add(Issue("warning", f"frame intervals differ ({', '.join(map(str, sorted(intervals)))}); "
                                 "speed-related settings and gap closing in frames mean different times"))
    return res
