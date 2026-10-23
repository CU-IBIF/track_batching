# TrackBatch

Batch-run **TrackMate** (Fiji) and **u-track** (MATLAB) particle tracking over many images, with
a GUI or a command line. Parameters can come from defaults, a saved parameter file, or a
**previous run of either tool**.

TrackBatch is an orchestrator. It does not reimplement the trackers, so it needs these installed:

| Engine    | Requires |
|-----------|----------|
| TrackMate | [Fiji](https://fiji.sc) (TrackMate is bundled). Runs headless, so no Fiji window opens. |
| u-track   | MATLAB R2016b or newer, plus [u-track 2.x](https://github.com/DanuserLab/u-track). Point TrackBatch at its `software` folder. |

## Download

Every push and pull request runs the tests and then builds Windows executables
(**Actions → latest run → Artifacts**):

* `TrackBatch.exe` is the GUI. Double-click it.
* `trackbatch-cli.exe` is the same program for scripts and terminals.

A GitHub Release with both files (zipped) is published automatically when:
* `main` gets a new version number in `pyproject.toml` (and `src/trackbatch/__init__.py`), so bumping the
  version and pushing to `main` creates the tag `v<version>` and its release, or
* you push a tag such as `v0.3.0`.

## GUI workflow

1. **Images:** drag files or folders from Explorer / Finder onto the list, or use **Add files… / Add folder…**
   (with a glob pattern such as `*.tif`, optionally recursive). Each image's width × height, channels, Z,
   frames, pixel size and bit depth are read as soon as it is added.
2. **Parameters:** pick the engine, then edit any value in the tree (double-click a value).
   * **Load from previous run…** accepts:
     * `trackmate.xml` / any TrackMate XML saved from Fiji (detector, tracker, spot/track filters)
     * u-track `.mat` result files (`detection.mat`, `tracking.mat`, or anything with
       `detectionParam`, `costMatrices`, `gapCloseParam`, `kalmanFunctions`)
     * u-track 2.x `movieData.mat` (read through MATLAB, because it holds MATLAB objects)
     * `trackbatch_run.json` / `trackbatch_params.json` from an earlier TrackBatch run. It can also
       re-add that run's images.
   * **Save parameters…** writes a JSON file you can reuse or pass to the CLI.
3. **Tools:** set the Fiji, MATLAB and u-track locations once. They are remembered in `~/.trackbatch/settings.json`.
   Images are checked against the current parameters (see [Pre-check](#pre-check)) when added, when the
   engine changes and before each run; **Check images** re-runs it after you change parameters.
   Select an image to see only its issues.
4. **Run:** pick an output folder and the number of parallel workers, then run. The Run tab shows
   each image's status and result/error. *Skip images that already succeeded* lets you resume an
   interrupted batch.

### Activity log and troubleshooting

* The **status bar** (bottom) shows what is happening now: header reading, *Running: 3/10 done ·
  now: movie_04.tif*, elapsed time and an estimated time remaining.
* The **activity log** (bottom panel, visible on every tab) records every action: images added, checks,
  parameter edits, the exact Fiji/MATLAB command, all of its output, exit codes and errors.
  **Show details** adds per-image header information. **Copy** puts the log on the clipboard
  for sharing.
* Every session is also written to `~/.trackbatch/logs/trackbatch_<date>_<time>.log` (**Open log file** /
  **Open log folder**). The last 30 sessions are kept. Unexpected errors are written there with a full traceback
  and shown in a dialog instead of failing silently.
* Each batch writes `<output>/_trackbatch/batch.log` with the commands and complete tool output.
* If a Fiji/MATLAB worker exits without printing any TrackBatch progress lines, the log says so. The
  usual causes are a wrong tool path or a Fiji/MATLAB start-up problem. The exact command is in the log, so you
  can run it in a terminal to see the full error.

## Command line

```bash
# TrackMate on every TIFF in a folder, overriding two settings
trackbatch run -e trackmate -i data/ --pattern "*.tif" -o results/ \
    --set detector.RADIUS=0.4 --set tracker.LINKING_MAX_DISTANCE=1.0

# Re-use the settings of a run done by hand in Fiji
trackbatch run --from-previous old/cells_01.xml -i new_data/ -o results2/

# u-track, using the parameters stored in a previous tracking result
trackbatch run -e utrack --from-previous old/tracking.mat -i data/ -o results_ut/ -w 2

# Re-run an earlier batch (same images and parameters), skipping finished images
trackbatch run --from-previous results/trackbatch_run.json --reuse-inputs -o results/ --skip-existing

# Inspect images against the parameters without running anything (exit code 1 if there are errors)
trackbatch check -e trackmate -i data/ --set detector.TARGET_CHANNEL=2

# Get a parameter file to edit (defaults or from a previous run)
trackbatch params -e utrack -o utrack_params.json
trackbatch params --from-previous old/cells_01.xml -o tm_params.json
trackbatch params --matlab-export old/movieData.mat -o ut_params.json   # u-track 2.x projects

# Remember tool locations
trackbatch config --fiji "C:/Fiji.app" --matlab "C:/Program Files/MATLAB/R2024b/bin/matlab.exe" \
    --utrack "C:/code/u-track/software"
```

Precedence is defaults → `--from-previous` → `--params file.json` → `--set key=value`.
Keys are dotted paths, and list items are addressed by index, for example
`tracking.costMatrices.1.parameters.maxSearchRadius=6`. Use `--dry-run` to write the worker
scripts and print the commands without running anything.

## Parameters

**TrackMate** keys are TrackMate's own settings keys, so values from TrackMate XML map one to one:

* `detector`: `DETECTOR_NAME` (`LOG_DETECTOR`, `DOG_DETECTOR`, `HESSIAN_DETECTOR`, `THRESHOLD_DETECTOR`,
  `MASK_DETECTOR`, `LABEL_IMAGE_DETECTOR`, or any installed detector key such as StarDist or Cellpose)
  plus that detector's settings.
* `tracker`: `TRACKER_NAME` (`SPARSE_LAP_TRACKER`, `SIMPLE_SPARSE_LAP_TRACKER`, `KALMAN_TRACKER`,
  `NEAREST_NEIGHBOR_TRACKER`, `OVERLAP_TRACKER`, …) plus its settings.
* `initial_spot_filter`, `spot_filters`, `track_filters`: entries look like
  `{"feature": "QUALITY", "value": 5, "is_above": true}`.
* `calibration`: overrides the pixel size and frame interval when the files are not calibrated.
  TrackMate distances are in physical units.
* `image.swap_z_t`: `"auto"` fixes movies whose frames were saved as Z-slices.
* `threads`: 0 means all cores.

Values are type-checked against the factory defaults inside Fiji, so `RADIUS=3` is passed as
a Double, as TrackMate expects. Unknown keys are dropped with a warning in the log.

**u-track** mirrors `scriptDetectGeneral.m` / `scriptTrackGeneral.m`:

* `detection.method`:
  * `subres_gaussian` uses `detectSubResFeatures2D_StandAlone` with `detection.detectionParam`.
  * `point_source` uses `pointSourceDetection` (the u-track 2.x default) with `detection.pointSource`.
* `tracking.gapCloseParam`, `tracking.costMatrices`, `tracking.kalmanFunctions`, `tracking.probDim`.
  Scalar `brownStdMult` / `linStdMult` in the gap-closing cost matrix are expanded to `timeWindow`-long vectors.
* `image.channel`, `image.first_frame`, `image.last_frame`.

u-track batches are 2D (as in the u-track standalone scripts). Multi-channel ImageJ TIFF
hyperstacks are supported. Other formats are read with Bio-Formats, which ships with u-track.

## Pre-check

Before every run, both the GUI and `trackbatch run` inspect the images, and `trackbatch check` does it on its own.
The report is saved as `precheck.txt` in the output folder. Errors stop the run before Fiji or MATLAB start.
The GUI asks whether to run anyway; on the command line, pass `--no-precheck`. Warnings are logged, and the run continues.

Images of **different sizes are fine**, because each image is analysed in full. The check looks for the
things that do go wrong in mixed batches:

| Check | Level |
|---|---|
| File missing or not a readable TIFF | error |
| Requested channel (`TARGET_CHANNEL` / `image.channel`) not in the image | error |
| u-track: 3D movie (the batch is 2D), or `first_frame` past the end of the movie | error |
| Only one time point | warning |
| TrackMate: no pixel calibration, so distances are in pixels (unless `calibration` is set) | warning |
| u-track: float pixels (converted to uint16), or bit depth below `detectionParam.bitDepth` | warning |
| Pixel size differs between images: for u-track (pixel units), or when a TrackMate calibration override forces one size | warning |
| Bit depth, channel count or frame interval differs between images | warning |
| Z-slices with one frame will be treated as time; sizes or pixel sizes differ in TrackMate (physical units) | info |

TIFF variants (ImageJ hyperstacks, OME-TIFF, plain multi-page TIFF) are inspected. Other formats
(`.nd2`, `.czi`, …) are listed as *not inspected*, because they are only opened by Bio-Formats at run time.

## Output

```
results/
  precheck.txt               # image table + issues found before the run
  trackbatch_run.json        # engine, parameters, images, per-image status (reload as "previous run")
  batch_summary.csv          # one row per image: status, counts, runtime, error
  _trackbatch/               # generated worker scripts, manifests and batch.log (handy for debugging)
  <image name>/
    trackbatch_params.json   # the parameters used for this image
    trackbatch_status.json
    # TrackMate:
    trackmate.xml            # open in Fiji (Plugins › Tracking › Load a TrackMate file)
    spots.csv  edges.csv  tracks.csv
    # u-track:
    detection.mat  tracking.mat      # standard u-track outputs (also usable as "previous run")
    detections.csv  tracks.csv  track_events.csv
```

## Parallel workers

Each worker is one Fiji or MATLAB process that handles its share of the images, so the startup
cost is paid once per worker. Every MATLAB worker uses a MATLAB licence seat, and every Fiji
worker uses its own Java heap, so set the worker count with your RAM and licences in mind.

## Development

```bash
pip install -e ".[dev]" tifffile
pytest
```

The worker-script tests run the real Fiji script under **Jython** (`JYTHON_JAR=…/jython-standalone-2.7.3.jar`)
and the MATLAB script under **GNU Octave**, both against stubbed TrackMate and u-track functions
(`tests/stubs`). When those runtimes are missing, the tests are skipped. CI
(`.github/workflows/build.yml`) installs both, runs everything, and builds the Windows
executables with PyInstaller only if the tests pass.
