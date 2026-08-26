# CCTV Project — Coding Guideline

This is the contract between everyone working on this codebase. It exists so that
when you open a file someone else wrote, it looks like a file you wrote.

Every rule here comes from something that actually happened in this repo. Where a
rule has a real example — a bug we shipped, a pattern that worked — it's quoted.

**How to use this:** read it once end to end. After that, treat it as reference.
The [Pull Request Checklist](#15-pull-request-checklist) at the bottom is the part
you'll use daily.

---

## 1. The one rule that matters most

**Dependencies point downward. Nothing ever imports from `ui`.**

```
ui/          → may import from anything below
alerts/      → may import detection, storage, core
recording/   → may import capture, storage, core
detection/   → may import capture, storage, core
capture/     → may import core
storage/     → may import core, paths
core/        → imports nothing from this project
paths.py     → imports nothing from this project
```

If you find yourself wanting `storage/camera_store.py` to import something from
`ui/`, stop — that's a sign the logic belongs in the caller, not the store.

We currently have **zero import cycles**. Keep it that way. CI checks it.

---

## 2. Project structure

```
main.py                     entry point only. Env setup, QApplication, show window.
cctv/
├── paths.py                every on-disk location
├── core/                   threading primitives (BackgroundWorker, WorkerManager)
├── storage/                SQLite. The data contract.
├── capture/                video decode threads
├── detection/              motion + object detection
├── alerts/                 rule matching + alert lifecycle
├── recording/              segment writing + event logging
└── ui/
    ├── app.py              MainWindow — the wiring hub
    ├── theme.py            colours and shared stylesheets
    ├── widgets/            reusable, no app knowledge
    ├── views/              full screens
    └── settings/           one file per Settings section
scripts/                    one-off tools. Never imported by the app.
models/                     model weights (gitignored)
tests/
data/                       runtime output (gitignored)
```

**Where does my new code go?**

| You're writing | Put it in |
|---|---|
| Something that runs on a background thread per camera | the relevant subsystem package, subclassing `core.worker.BackgroundWorker` |
| A new Settings screen | `ui/settings/<name>.py` — a new file, not an existing one |
| A reusable Qt widget with no knowledge of cameras | `ui/widgets/` |
| A full screen the user navigates to | `ui/views/` |
| Pure logic with no I/O and no Qt | its subsystem package — and write tests for it |
| A path to a file on disk | `cctv/paths.py`. Nowhere else. |
| A one-off migration or maintenance tool | `scripts/` |

---

## 3. Naming

| Thing | Convention | Example |
|---|---|---|
| Modules | `snake_case`, singular | `camera_store.py` |
| Classes | `PascalCase` | `MotionWorker` |
| Functions / methods | `snake_case` | `get_zone_masks` |
| Constants | `UPPER_SNAKE` at module level | `DETECTION_COOLDOWN_SECONDS` |
| Private | single leading underscore | `_denormalize_polygon` |
| Qt slots | `_on_<thing>_<event>` | `_on_camera_picked` |
| Booleans | read as a question | `motion_enabled`, `is_detecting` |

**Units go in the name.** `DETECTION_COOLDOWN_SECONDS`, not `DETECTION_COOLDOWN`.
`file_size_bytes`, not `file_size`. `detected_at_iso`, not `detected_at`. We already
do this well — keep it up. It has prevented more bugs than any other convention here.

**Established suffixes, use them consistently:**

- `*Worker` — one background thread, one camera. Has `start()`/`stop()`.
- `*Manager` — owns a dict of workers keyed by camera id.
- `*Store` — owns a SQLite file. The only thing that touches that DB.
- `*SectionPanel` — one screen inside Settings.
- `*Status` — a class of string constants (`StreamStatus.CONNECTED`).

---

## 4. The data contract

### The camera dict

`CameraStore._row_to_camera()` produces the dict that **detection, alerts,
recording and six UI files all read**. It is the single most load-bearing shape in
the codebase.

```python
{
    "id": str,
    "name": str,
    "url": str,
    "type": str,                                  # "rtsp" | ...
    "motion_enabled": bool,
    "motion_sensitivity": str,                    # "low" | "medium" | "high"
    "object_detection_enabled": bool,
    "object_detection_mode": str,                 # "on_motion" | "continuous"
    "object_detection_classes": [str],
    "object_detection_class_confidence": {str: float},
    "alert_rules": [ {...} ],
    "zones": [ {"id", "name", "points", "detection_enabled"} ],
}
```

Rules:

- **Adding a key is safe.** Renaming or removing one breaks four people at once.
  Announce it before you do it.
- **Never read a key that isn't in this list.** We shipped a bug doing exactly
  that:

  ```python
  # motion_detector.py — REAL BUG, sensitivity silently did nothing for months
  threshold = camera.get("motion_threshold", DEFAULT_MOTION_THRESHOLD)
  ```

  `motion_threshold` was never a column and never in the dict, so `.get()`
  cheerfully returned the default every time and Low/Medium/High were identical.
  **`.get()` with a default will hide your typo forever.** If a key is required,
  index it (`camera["motion_enabled"]`) so a mistake raises immediately.

### Coordinate spaces

- **Zone points and detection bboxes are normalised `0.0–1.0`**, always relative to
  the *source frame*, never to a widget or canvas size.
- Convert to pixels only at the point of use, using that frame's actual
  dimensions. `detection/motion_detector.py::_denormalize_polygon` is the one
  place that conversion lives — use it, don't reimplement it.

### Timestamps

- **Stored in SQLite:** ISO 8601 strings via `datetime.now().isoformat(timespec="seconds")`.
  String comparison sorts correctly, which is why range queries work.
- **In memory, for elapsed-time logic:** `time.time()` floats.
- Don't mix them. Don't store floats. Don't do arithmetic on ISO strings.

### Don't repurpose columns

We currently store an alert's rule name in the `zone_id` column and its duration in
the `confidence` column. It's documented, and it's still wrong: `confidence` is
supposed to be 0.0–1.0, and any future query or export is booby-trapped.

**If you need a new field, add a column.** `ALTER TABLE` is cheap. Overloading is
a permanent tax on everyone who reads that table later.

---

## 5. Threading

This app runs up to six threads per camera. Threading confusion is the most
expensive kind of bug here, so these rules are strict.

### Every background thread uses `core.worker.BackgroundWorker`

Don't hand-roll a thread. Subclass it and implement `tick()`:

```python
class MotionWorker(BackgroundWorker):
    INTERVAL_SECONDS = 0.2
    LOG_TAG = "motion"

    def tick(self):
        frame = self.stream_manager.get_frame(self.cam_id)
        if frame is None:
            return
        self._process_frame(frame)
```

You get `start()`, `stop()`, exception-safe looping, immediate shutdown and a
named thread for free.

### Never `time.sleep()` in a worker loop

Use `self._wait_or_stop(seconds)`. It returns `True` early when stop is requested.
With `sleep()`, shutdown waits out a full interval **per worker** — at six workers
per camera that's a visibly frozen app on close.

### Lock discipline

- One lock per piece of shared state, named for what it guards: `_slots_lock`,
  `_log_lock`.
- **Hold it for as short a time as possible.** Copy out, release, then work:

  ```python
  # good
  with self._lock:
      result = self._latest_result
  expensive_thing(result)

  # bad — blocks every reader for the duration
  with self._lock:
      expensive_thing(self._latest_result)
  ```

- **Never call another object's method while holding a lock.** That's how
  deadlocks start.
- Never do I/O (SQLite, disk, network) inside a lock.

### Don't touch a worker's resources from another thread

Real bug pattern, from `video_stream.py`:

```python
# BAD — releases the capture from the caller's thread
self._thread.join(timeout=2)
if self._cap is not None:
    self._cap.release()      # worker may still be blocked inside cap.read()
```

If the join times out, you free a `VideoCapture` that FFmpeg is still inside. That
is a segfault, not an exception. **A resource is owned by the thread that created
it and released by that same thread.** `stop()` signals and joins; cleanup that
must happen elsewhere goes in `on_stop()`, and only for things that are safe there.

### Documenting thread-safety

Every method that can be called from a thread other than its owner's says so:

```python
def invalidate_zones(self):
    """Flags the mask cache stale. Safe to call from any thread —
    the GUI thread calls this; the worker reads the flag on its own."""
```

If you can't state which thread calls a method, you don't yet understand the code
well enough to change it.

---

## 6. The storage boundary

**Only `storage/` modules open a SQLite connection.** Everyone else calls methods.

This is already the convention and it's a good one. Two additions:

- **Config is not hot data.** Don't call `get_camera()` inside a loop that runs
  many times a second. We currently do this in three workers — 12 full nested
  reads per second per camera, ~10 queries each. Read once per tick at most; cache
  where you can.

- **Never call a store method per item in a loop.** Real example:

  ```python
  # BAD — a fresh SQLite connection per detected bounding box
  for class_name, conf, xyxy in detections:
      threshold = self.camera_store.get_class_confidence(self.cam_id, class_name)

  # GOOD — the dict you already have carries it
  thresholds = camera.get("object_detection_class_confidence", {})
  for class_name, conf, xyxy in detections:
      threshold = thresholds.get(class_name, CameraStore.DEFAULT_CLASS_CONFIDENCE)
  ```

### Paths

All of them come from `cctv/paths.py`. Do not call `os.path.expanduser("~")`,
`os.path.abspath(".")` or `sys.executable` anywhere else. We had five different
conventions across five files and nobody could find their own data.

---

## 7. Error handling

### Catch narrowly, or explain why not

```python
# BAD — the tuple is meaningless, Exception already covers cv2.error
except (cv2.error, Exception):

# GOOD
except cv2.error as exc:
```

A broad `except Exception` is acceptable in exactly two places, and both must log:

1. A worker's top-level `tick()` wrapper (already handled by `BackgroundWorker`).
2. A best-effort side task where failure genuinely shouldn't stop the main job —
   thumbnail writes, alert channel sends. Comment it as such.

### Never swallow silently

```python
except OSError:
    pass          # BAD: you will spend an evening finding this
```

Log with the subsystem tag, or let it raise. If it's truly ignorable, say why in a
comment on the same line.

### Validate at the boundary, trust inside

`CameraStore` validates on write (`_validate_hhmm`, `_validate_points`,
sensitivity enums). That's the right place. Code downstream of the store shouldn't
re-validate — but should degrade gracefully on malformed data rather than crash,
because data can be hand-edited:

```python
if len(points) < 3:
    continue  # degenerate zone — skip rather than crash
```

### Logging format

Until we adopt `logging`, use the existing convention:

```python
print(f"[{subsystem}] cam={cam_id} {what happened}: {exc}")
```

Always include the subsystem tag and, where relevant, the camera id. A log line
you can't trace to a camera is nearly useless with 16 cameras running.

**Failures the user needs to know about must reach the UI.** A `print()` to stdout
is invisible to someone running a GUI app. Object detection currently fails to load
its model with nothing but a stdout line — don't repeat that.

---

## 8. Comments and docstrings

This codebase has unusually thorough docstrings. That's a real asset, and it has
also become a real liability, because several have drifted from the truth:

- `video_stream.py` and `grid_layout.py` said **Tkinter**. It's PyQt6.
- `object_detector.py` documented `_best_allowed_detection` and
  `_collect_allowed_detections` — neither function exists.
- `motion_detector.py` documented a `motion_threshold` storage field that was
  never implemented (and that's the sensitivity bug in §4).

**A confidently wrong comment is worse than no comment.** So:

### Write the *why*, not the *what*

```python
# BAD — restates the code
# Loop over the zones and build a mask for each
for zone in zones:

# GOOD — explains a decision the code can't
# Rebuilding masks with fillPoly every frame is wasted work when the
# polygon hasn't changed, so cache per (zone signature, frame size).
```

### Don't put change history in docstrings

This belongs in git, not in a docstring that must be maintained forever:

```python
# DON'T
"""Bug fix: this used to just assume the writer succeeded..."""
"""NOTE: this used to also define PRESENCE_TIMEOUT_SECONDS..."""
"""Drop-in replacement for the JSON-backed CameraStore..."""
```

Put it in the commit message. `git log -p <file>` and `git blame` already answer
"why did this change" better than a docstring ever will, and they don't go stale.

Keep the *why that is still true*. Delete the archaeology.

### Docstring shape

```python
def find_segment_for_timestamp(self, camera_id, timestamp_iso):
    """Return the segment whose [start_time, end_time) window contains
    timestamp_iso, or None.

    end_time IS NULL means "still recording", so a lookup against the
    currently-open segment still resolves.
    """
```

One-line summary, blank line, then only the non-obvious parts. Don't document
parameters whose names already say it.

### When you change code, change its comments

Non-negotiable. If a reviewer sees updated code above a stale comment, that's a
change request.

---

## 9. UI conventions

- **Colours come from `ui/theme.py`.** No hex literals in widget code. Append new
  constants; don't reorder existing ones (reordering causes merge conflicts).
- **Views don't reach into other views.** Cross-view updates go through
  `MainWindow` — see the `notify_zones_changed` hub pattern. Keep that shape.
- **Signals for child → parent, method calls for parent → child.**
- **`ui/widgets/` knows nothing about cameras, stores or detection.** It takes data
  and paints it. If a widget imports `camera_store`, it's a view, not a widget.
- **Keep `MainWindow.__init__` diffs to one or two lines.** It's the highest-
  conflict file in the repo; every feature wants to add a line there.

### The poll loop is sacred

`MainWindow._poll` runs at ~30 Hz. Anything you add there runs 30 times a second
across every visible tile.

- No SQLite queries.
- No disk I/O.
- No image encode/decode beyond what's already there.
- Prefer `cv2.resize` over Qt's `SmoothTransformation` for scaling frames.
- Use the frame-counter guard (`CameraTile.update_frame`) so unchanged frames skip
  the work entirely. That pattern is good — follow it for anything new.

---

## 10. Performance rules

Only three, but they cover most of what's gone wrong:

1. **Vectorise NumPy. Never loop over rows in Python.** The ONNX postprocessor
   loops 8,400 anchor rows calling `np.argmax` on each — measured at 12.5 ms
   versus 1.1 ms vectorised, per inference call, per region, per camera. If you
   write `for row in array:`, stop and find the array operation.

2. **Nothing per-frame that could be per-change.** Config, zone masks, thresholds,
   stylesheets — compute on change, cache, invalidate explicitly. The mask cache in
   `motion_detector.py` is the model to copy.

3. **Measure before optimising, and put the number in the PR.** "Feels faster"
   isn't reviewable. `time.perf_counter()` around the thing, before and after.

---

## 11. Adding things — recipes

### A new Settings section

1. New file `ui/settings/<name>.py`
2. Subclass the shared camera-scoped base (once `base.py` lands; until then follow
   `zones.py`'s shape)
3. One line in `ui/settings/panel.py` to register it
4. One line in `ui/settings/__init__.py` to export it

**Never edit another section's file to add yours.** That's the whole point of the
split.

### A new background worker

1. Subclass `BackgroundWorker`, set `INTERVAL_SECONDS` and `LOG_TAG`
2. Implement `tick()`; cleanup in `on_stop()` if needed
3. Subclass `WorkerManager`, implement `_make_worker()`
4. Wire start/stop in `MainWindow` — and add it to `closeEvent` in the right order

**Shutdown order matters.** `closeEvent` currently stops event_logger and alerts
*before* recording, because both tag their closing row with
`recording.get_current_segment_id()`. If you add a worker with a similar
dependency, document it there.

### A new store method

1. It goes in the `*Store` class, not the caller
2. Validate inputs; raise `ValueError` with a message naming the field
3. Return plain dicts/lists — no SQLite objects escape the store
4. Don't return the full camera dict from a setter unless the caller needs it
   (every setter currently does a ~10-query re-read to return a dict most callers
   discard)

---

## 12. Testing

Not everything is testable — Qt widgets and live streams mostly aren't. Some
things very much are, and those are where bugs hide:

**Must have tests:**

- Pure logic: `alert_matcher.py`, `grid_layout.py`, geometry helpers
- `CameraStore` / `EventStore` methods (use a temp DB path — the stores take
  `path=`, which is exactly why)
- Anything with edge cases around time, midnight, empty lists, or zero

**Test naming:** `test_<what>_<condition>_<expectation>`

```python
def test_window_crossing_midnight_includes_early_morning():
def test_disabled_rule_never_matches():
```

Run `pytest` before pushing. If you fix a bug, add the test that would have caught
it — in the same PR.

---

## 13. Git workflow

### Branches

`<type>/<short-description>` — `fix/motion-sensitivity`,
`feat/recording-toggle`, `refactor/worker-base`

### Commits

```
<area>: <what changed, imperative mood>

<why, if not obvious. Wrap at 72.>
```

```
detection: map motion_sensitivity to a real pixel threshold

The UI wrote motion_sensitivity but the worker read motion_threshold,
which no column produced — so Low/Medium/High all resolved to the
same default. Adds the mapping and a test.
```

Areas: `storage`, `capture`, `detection`, `alerts`, `recording`, `ui`, `core`,
`build`, `docs`.

### Pull requests

- **One concern per PR.** A refactor and a bug fix in one PR is unreviewable.
- Under ~400 lines of real change where possible. Split by the recipes in §11.
- Say what you tested and how. "Ran with 2 RTSP cameras for 10 min" is a real
  answer; "should work" isn't.
- Two people should never have open PRs touching `ui/app.py` at the same time.
  Coordinate in chat first.

### Before you push

```bash
ruff check --fix .    # lint + import order
pytest
```

Import ordering is enforced because unsorted import blocks are merge magnets — with
consistent ordering, git merges them cleanly instead of conflicting.

---

## 14. Things we've agreed not to do

- Commit anything into `data/` or `models/*.onnx`
- Add a dependency without discussing it (we deliberately removed torch and
  ultralytics to get the install down — don't quietly add them back)
- Use `localStorage`-style hidden global state; pass dependencies in via `__init__`
- Reformat a file you're not otherwise changing (it hides the real diff)
- Leave commented-out code; git remembers it
- Add a `TODO` without a name and a date: `# TODO(sam, 2026-09): ...`

---

## 15. Pull request checklist

Copy this into your PR description.

```markdown
- [ ] Imports point downward; no new cycles
- [ ] No new hardcoded paths (used cctv/paths.py)
- [ ] No SQLite calls inside a per-frame or per-item loop
- [ ] New background work subclasses BackgroundWorker
- [ ] Shared state is locked; locks held briefly, no I/O inside
- [ ] Comments/docstrings updated to match the code I changed
- [ ] No change-history narrative added to docstrings
- [ ] Units in names (_seconds, _bytes, _iso)
- [ ] New camera-dict keys announced to the team
- [ ] Tests added for pure logic / bug fixes
- [ ] `ruff check .` and `pytest` pass
- [ ] Tested how: ___________
```

---

## Changing this document

These are conventions, not laws, and some will turn out to be wrong. If a rule
here is getting in your way, open a PR against this file and argue the case — that
is much better than quietly ignoring it, because a guideline nobody follows is
worse than no guideline at all.
