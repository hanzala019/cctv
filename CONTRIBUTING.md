# Working on this codebase as a team

## The layout

```
main.py                     entry point only -- 70 lines, rarely changes
cctv/
├── paths.py                every on-disk location, one place
├── core/                   shared threading primitives
│   ├── worker.py           BackgroundWorker base
│   └── manager.py          WorkerManager base
├── storage/                SQLite. The data contract.
│   ├── camera_store.py
│   ├── event_store.py
│   └── app_prefs.py
├── capture/
│   └── video_stream.py     RTSP/HTTP decode threads
├── detection/
│   ├── motion_detector.py  MOG2 + zone masks
│   └── object_detector.py  ONNX YOLOv8
├── alerts/
│   ├── alert_matcher.py    pure logic, no I/O -- test this heavily
│   └── alert_manager.py    lifecycle + channels
├── recording/
│   ├── recording_manager.py
│   └── event_logger.py
└── ui/
    ├── app.py              MainWindow -- the wiring hub
    ├── diagnostics.py
    ├── theme.py
    ├── widgets/            reusable, no app knowledge
    │   ├── video_label.py  painting + overlays
    │   ├── camera_tile.py  one tile
    │   ├── icons.py
    │   └── grid_layout.py  pure math
    ├── views/              full screens
    │   ├── grid_view.py
    │   ├── single_view.py
    │   ├── detection_panel.py
    │   └── zone_editor.py
    └── settings/           ONE FILE PER SECTION
        ├── panel.py        the shell + section switcher
        ├── base.py         (to add) shared camera-picker panel
        ├── cameras.py
        ├── zones.py
        ├── object_detection.py
        ├── alerts.py
        ├── recordings.py
        ├── events.py
        ├── dialogs.py      shared dialogs
        └── formatting.py   shared size/duration/date formatters

scripts/                    one-off tools, not imported by the app
models/                     yolov8n.onnx (gitignored, downloaded)
tests/
data/                       runtime output (gitignored)
```

## Why this shape

The rule is **dependencies point downward**: `ui` may import from
`detection`, `detection` may import from `storage` and `core`, and
nothing ever imports from `ui`. There are currently zero import cycles;
please keep it that way (`python3 -m pyflakes` plus the cycle check in
CI will tell you).

The biggest win for parallel work isn't the folders, it's that
`settings_panel.py` (2,410 lines) is now nine files of 200–660 lines.
Two people editing different Settings sections no longer touch the same
file at all.

## Ownership and conflict zones

| Area | Conflict risk | Notes |
|---|---|---|
| `ui/settings/*.py` | **Low** | One file per section. Claim a section, work freely. |
| `detection/`, `recording/`, `alerts/` | **Low** | Independent subsystems, talk only via managers. |
| `storage/` | **High** | The camera dict shape is everyone's contract. Discuss before changing. |
| `core/` | **High** | Changes ripple into all six workers. One owner. |
| `ui/app.py` | **Highest** | Every new feature wants a line in `MainWindow.__init__`. Keep diffs to 1–2 lines. |
| `ui/theme.py` | Medium | Append colours, don't reorder. |

Update `CODEOWNERS` with real handles so reviews auto-route.

## Rules that actually prevent conflicts

1. **Never change the camera dict shape without telling everyone.**
   `storage/camera_store.py`'s `_row_to_camera()` output is consumed by
   detection, alerts, recording and six UI files. Adding a key is safe;
   renaming or removing one breaks four people at once.

2. **Add sections, don't edit the shell.** A new Settings section = a
   new file + one line in `panel.py` + one line in `__init__.py`.

3. **Import blocks are merge magnets.** `ruff`'s import sorting (`I`
   rules, already configured in `pyproject.toml`) means everyone's
   imports land in the same order, so git can merge them cleanly.
   Run `ruff check --fix` before committing.

4. **Line endings are normalised** via `.gitattributes`. The old files
   were CRLF; without this, one person on Windows produces a
   whole-file diff on every save and every PR conflicts with every
   other PR.

5. **Never commit `data/` or `models/*.onnx`.** Recording writes tens of
   GB per camera per day. Already in `.gitignore` — don't override it.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
# download yolov8n.onnx into models/  (see README)
python main.py
```

```bash
pytest                 # tests
ruff check .           # lint + import order
ruff check --fix .     # autofix
```

## Migrating your existing data

Paths moved into a single `data/` directory. Move your existing files:

```bash
mkdir -p data
mv cctv_viewer_cameras.db          data/cameras.db
mv ~/.cctv_viewer_events.db        data/events.db
mv ~/.cctv_viewer_alerts.log       data/alerts.log
mv ~/.cctv_viewer_prefs.json       data/prefs.json
mv cctv_viewer_footage             data/footage
mv event_thumbnails                data/event_thumbnails
```

Segment rows in `events.db` store absolute `file_path`s, so after moving
footage you'll need to rewrite them:

```sql
UPDATE segments SET file_path = replace(file_path, '/old/abs/path', '/new/abs/path');
```

## Still to do in this restructure

The move is mechanical and complete — every module compiles, pyflakes is
clean, there are no import cycles, and 28 tests pass. Three follow-ups
that need real edits rather than file moves:

1. **Point the stores at `cctv/paths.py`.** `camera_store.py`,
   `event_store.py`, `recording_manager.py`, `alert_manager.py` and
   `app_prefs.py` still each compute their own paths. Replace those with
   `paths.cameras_db()` etc. and delete the three copies of
   `_project_dir()`.
2. **Rebase the workers onto `core/worker.py` and `core/manager.py`.**
   The bases are written and documented; each of the six workers needs
   its `start`/`stop`/`_run`/`_wait_or_stop` deleted and its loop body
   renamed to `tick()`. Roughly −250 lines.
3. **Extract `ui/settings/base.py`.** The camera-picker + `refresh()` +
   `_on_camera_picked()` + `_load_camera()` block is still copy-pasted
   across five section panels. Roughly −200 lines.

These are independent of each other — three people can take one each.
