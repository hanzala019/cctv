# CCTV Viewer

A desktop application for viewing multiple IP cameras at once, with motion
detection, object detection, zone-based alerts and continuous recording.

Built with PyQt6, OpenCV and ONNX Runtime. Runs entirely on the local machine —
no cloud service, no account, no network calls beyond the camera streams
themselves.

---

## What it does

- **Live grid view** of every configured camera, plus a single-camera view
- **Motion detection** per camera, with adjustable sensitivity
- **Detection zones** — draw polygons so only the areas you care about trigger
- **Object detection** (YOLOv8n via ONNX Runtime) with per-class confidence
  thresholds, running either continuously or only when motion is seen
- **Alert rules** per camera: time windows (including windows that cross
  midnight), triggered by motion or by specific object classes
- **Continuous recording** to 30-minute MP4 segments, with automatic cleanup
- **Event log** of motion and detection events, with thumbnails

---

## Requirements

- **Python 3.12 or 3.13**
- Linux, macOS or Windows
- Any camera reachable over RTSP or HTTP

The Python floor is set by numpy and onnxruntime, not by our code. Python 3.11
and earlier will not resolve `requirements.txt`.

---

## Setup

### 1. Create a virtual environment

```bash
python -m venv .venv

source .venv/bin/activate      # Linux / macOS
.venv\Scripts\activate         # Windows
```

### 2. Install dependencies

```bash
pip install -r requirements-dev.txt   # runtime deps + test tools
```

Runtime only, for a deployment machine:

```bash
pip install -r requirements.txt
```

### 3. Get the detection model

**The app runs without this, but object detection will silently do nothing.**
The weights are not in the repository — they're large, and they carry a licence
that isn't ours to redistribute (see [Licensing](#licensing)).

`yolov8n.onnx` is produced by exporting Ultralytics' PyTorch weights. Do this
once, in a **separate** virtual environment — `ultralytics` pulls in PyTorch,
which is exactly the multi-gigabyte dependency this project deliberately avoids:

```bash
python -m venv /tmp/export-env
source /tmp/export-env/bin/activate

pip install ultralytics
yolo export model=yolov8n.pt imgsz=640 format=onnx opset=12

deactivate
```

That writes `yolov8n.onnx` into the current directory. Move it into `models/`:

```bash
mv yolov8n.onnx /path/to/this/repo/models/
```

`imgsz=640` matters — the postprocessing in
`cctv/detection/object_detector.py` assumes a 640×640 input and an
`(1, 84, 8400)` output tensor. `opset=12` or higher is required for
compatibility.

### 4. Run

```bash
python main.py
```

Add your cameras from **Settings → Cameras**.

---

## Where your data goes

Everything the application writes lives in one directory:

```
data/
├── cameras.db            camera config, zones, alert rules
├── events.db             motion / detection / alert events, recording segments
├── alerts.log            human-readable alert log
├── prefs.json            UI preferences
├── footage/<camera_id>/  recorded MP4 segments
└── event_thumbnails/     detection event thumbnails
```

Override the location with an environment variable — useful for putting footage
on a separate disk, or for running a second instance:

```bash
CCTV_DATA_DIR=/mnt/storage/cctv python main.py
```

### Migrating from an older checkout

Earlier versions scattered these files across five different locations. To keep
your existing data:

```bash
mkdir -p data
mv cctv_viewer_cameras.db      data/cameras.db
mv ~/.cctv_viewer_events.db    data/events.db
mv ~/.cctv_viewer_alerts.log   data/alerts.log
mv ~/.cctv_viewer_prefs.json   data/prefs.json
mv cctv_viewer_footage         data/footage
mv event_thumbnails            data/event_thumbnails
```

Segment rows store **absolute** paths, so after moving footage you must rewrite
them or the Recordings panel will show every clip as missing:

```bash
sqlite3 data/events.db \
  "UPDATE segments SET file_path = replace(file_path, '/old/abs/path', '/new/abs/path');"
```

Check it worked:

```bash
sqlite3 data/events.db "SELECT file_path FROM segments LIMIT 3;"
```

---

## Known issues

Read this before deploying anywhere real. These are known, not hidden.

### Recording is unconditional and will fill your disk

Every camera records 24/7 from the moment it is added. There is no per-camera
off switch, and retention is hardcoded to 14 days in
`cctv/recording/recording_manager.py`.

At 720p that is roughly **20 GB per camera per day** — about **2.4 TB for eight
cameras** before cleanup begins. Point `CCTV_DATA_DIR` at a disk that can take
it, and watch free space.

### Camera credentials are stored and displayed in clear text

RTSP URLs typically embed credentials (`rtsp://admin:password@192.168.1.50`).
They are stored unencrypted in `cameras.db` and **shown in full in the camera
table** in Settings → Cameras. Be careful screen-sharing or screenshotting that
screen.

### Motion sensitivity currently has no effect

The Low / Medium / High control writes a `motion_sensitivity` value, but the
detector reads a `motion_threshold` value that nothing produces — so all three
settings behave identically. Fix pending.

### Other

- Object detection uses class-agnostic NMS, so heavily overlapping objects of
  different classes can suppress one another.
- There is no authentication. Anyone with access to the machine has access to
  every camera and all recorded footage.

---

## Development

Full conventions are in **[GUIDELINE.md](GUIDELINE.md)**. Team structure and
ownership are in **[CONTRIBUTING.md](CONTRIBUTING.md)**. The short version:

```bash
ruff check .                     # lint + import order
ruff check --fix .               # autofix
pytest                           # tests
python scripts/check_imports.py  # no import cycles, no layering violations
```

CI runs all of these on every pull request, on Python 3.12 and 3.13.

### Project layout

```
main.py             entry point only
cctv/
├── paths.py        every on-disk location
├── core/           shared threading primitives
├── storage/        SQLite stores
├── capture/        video decode
├── detection/      motion + object detection
├── alerts/         rule matching + alert lifecycle
├── recording/      segment writing + event logging
└── ui/             PyQt6 widgets, views and settings sections
scripts/            maintenance tools
tests/
```

Dependencies point downward, and nothing outside `ui/` imports from `ui/`.
`scripts/check_imports.py` enforces both.

---

## Roadmap

Ordered by priority, not by ambition.

1. Per-camera recording toggle, configurable retention, disk-space guard
2. Mask camera credentials in the UI
3. Fix motion sensitivity
4. Move the remaining workers onto `core/worker.py` and `core/manager.py`
5. Extract the shared camera-picker base for Settings sections
6. Vectorise ONNX postprocessing (measured ~12× faster on that stage)
7. Cache camera config instead of re-reading it several times a second
8. Decide: single-machine desktop app, or networked multi-user?
9. Users, authentication, authorization — only meaningful after (8)

Longer term, and honestly out of reach without a GPU budget: object tracking,
custom-trained models, position prediction, and a natural-language assistant
for querying event history. Something to revisit when we are less broke.

---

## Licensing

**This matters and is easy to get wrong.**

Ultralytics YOLO models — including `yolov8n.onnx` exported as described above,
and any model you fine-tune yourself — are distributed under **AGPL-3.0**, with
a paid Enterprise License as the alternative. Ultralytics' stated position is
that internal company use requires one or the other: either open-source your
entire project under AGPL-3.0, or buy an Enterprise License.

Removing the `ultralytics` pip dependency does **not** avoid this. The exported
weights are still an Ultralytics model, and the licence follows the weights.

If this project stays a personal, hobby or open-source effort, AGPL-3.0 is
fine. If it ever becomes commercial, internal-business, or closed-source, get
proper legal advice before shipping. Permissively licensed detectors exist if
that becomes a constraint.

Nothing here is legal advice.
