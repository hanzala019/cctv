"""
alert_manager.py

Phase 5: alert dispatch. This is the "different mechanism" the roadmap
flagged as needed before Phase 5 started -- alerts fire without a
user-facing view to push to, so unlike notify_zones_changed's hub
pattern (find the live widget, call a setter on it), this mirrors
MotionWorker/ObjectDetectionWorker's shape instead: one background
AlertWorker per camera, ticking independently of whichever view (or no
view) is currently on screen, owned by an AlertManager with the same
start/stop-per-camera lifecycle every other manager in this app has.

Trigger sources
---------------
AlertWorker doesn't decode frames or run any detection itself -- it's
purely a downstream consumer of state two other systems already
compute:

    - motion: MotionManager.get_result(cam_id). Camera-wide "is there
      motion right now" is `result.motion or any(result.zones.values())`
      -- NOT just result.motion. Reason: Phase 3's zone-priority rule
      means that once a camera has any zone with detection_enabled,
      result.motion (the whole-frame bool) is permanently False for
      that camera -- only result.zones matters. Alert rules are
      per-camera (not per-zone, per the Phase 5 design discussion), so
      "any motion on this camera" has to OR both halves together or a
      zoned camera would silently never fire a motion alert.

    - object_class: ObjectDetectionManager.get_present_classes(cam_id).
      A set of every class currently believed present on this camera
      (across every zone/whole-frame region), backed by
      object_detector.py's own per-instance presence-slot tracking
      (ABSENCE_TIMEOUT_SECONDS = 5s re-arm window) -- not a recency
      guess off a single latest event the way this used to work.

      Deliberately still per-RULE, not per-instance: object_detector.py
      itself fires a separate DetectionEvent for each individual new
      instance (2 people arriving fires 2 events), but an alert rule
      here only ever has one open START/END lifecycle regardless of
      how many instances of a matching class are present -- "is this
      rule's condition true right now" stays a single yes/no per rule,
      the same coarser-grained concept Phase 5 designed. Per-instance
      alerting lives in object_detector.py's own event stream (and
      from there, the DetectionSidePanel / event_store), not
      duplicated again at the rule-matching layer.

Alert lifecycle (start / end, with duration)
---------------------------------------------
Per the design discussion: a continuous presence does NOT re-alert
every tick or on a timer -- it alerts once when a rule starts matching
(ALERT START, logged immediately) and once when it stops matching
(ALERT END, logged with the elapsed duration). If the same trigger
happens again later (motion clears then returns; a detection lapses
past PRESENCE_TIMEOUT_SECONDS then a new one arrives), that's a new,
independent START/END pair. This is tracked per (camera, rule_id) via
_AlertState -- deliberately per *rule*, not per raw trigger, since two
overlapping rules matching the same underlying motion are two
independent alert lifecycles that should each get their own
START/END pair and duration (per the "fire once per matching rule"
design decision), not be collapsed into one.

Channels
--------
AlertChannel is a tiny interface (just .send(text)) so today's
log-file-only channel doesn't block adding desktop notifications,
email, or push later (Phase 5's roadmap explicitly suggests starting
with the simplest channel before adding network dependencies).
AlertManager holds a list of channels and broadcasts every alert line
to all of them.

Phase 7 addition: event_store integration
------------------------------------------
Phase 5 originally shipped with "no GUI visibility into alert history
(by design) -- deliberately deferred to Phase 7, since a bounded
in-memory panel would just be thrown away once persistent, filterable
history exists." Phase 7 built that persistent history (event_store's
`events` table), so this closes the loop: AlertWorker now also writes
each START/END edge into event_store directly, alongside (not instead
of) dispatching through the existing AlertChannel list. This is a
separate, parallel write path rather than a new AlertChannel
implementation -- the channel interface only carries a preformatted
log line (just text), but event_store needs structured fields
(camera_id, timestamp, segment_id), so bolting a "structured channel"
onto that interface would have meant changing it for every existing
channel too. Direct calls to event_store, gated on it being provided,
keeps LogFileAlertChannel (and any future channel) completely
unaffected.

Schema reuse note: the `events` table has no column for a rule's name
or a lifecycle's duration -- both were designed around motion/
detection rows, which don't have either concept. Rather than migrate
the schema for one new row type, alert rows reuse existing columns
with a documented convention:
    - detection_class: "alert_start" / "alert_end" (parallel to
      event_logger.py's "motion_start"/"motion_end")
    - zone_id: the rule's name (this column is otherwise a zone's id
      or None; it has no FK constraint, so this is safe, just a
      convention EventsSectionPanel's display code needs to know about)
    - confidence: on the "alert_end" row only, the lifecycle's
      duration in seconds (this column is otherwise a 0.0-1.0
      detection confidence; same repurposing idea, documented here and
      wherever it's read)
"""

import threading
import time
from datetime import datetime

from core import paths
from core.alerts.alert_matcher import active_motion_rules, active_object_class_rules
from core.worker.manager import WorkerManager
from core.worker.worker import BackgroundWorker

# How often AlertWorker checks state. Alerts aren't as latency-
# sensitive as motion/detection (a fraction of a second of extra delay
# before an alert fires is irrelevant), so this doesn't need to match
# MotionWorker/ObjectDetectionWorker's faster 0.2s cadence.
TICK_INTERVAL_SECONDS = 0.5

# NOTE: this used to also define PRESENCE_TIMEOUT_SECONDS -- a 25s
# recency guess for "is this class still present," since
# ObjectDetectionManager only ever exposed a single latest event with
# no real presence tracking behind it. That's no longer needed:
# object_detector.py's multi-instance presence-slot rework gives it
# genuine presence state (ABSENCE_TIMEOUT_SECONDS-based, 5s), so
# _tick_object_class below just asks for it directly via
# get_present_classes() instead of re-deriving a guess from timestamps
# here.


class AlertChannel:
    """Minimal interface every alert channel implements."""

    def send(self, text):
        raise NotImplementedError


class LogFileAlertChannel(AlertChannel):
    """Simplest possible channel: append one line per alert lifecycle
    edge (START/END) to a plain text file. Same home-dir convention as
    camera_store.py's JSON file. Phase 7 added a second, parallel
    history path (event_store, see AlertWorker._log_to_event_store)
    rather than replacing this one -- this stays deliberately dumb
    (append-only text) as the simplest possible channel, exactly as
    Phase 5 designed it."""

    # Resolved lazily so CCTV_DATA_DIR is honoured; see core/paths.py.
    DEFAULT_LOG_PATH = None

    def __init__(self, path=None):
        self.path = path or paths.alert_log()
        self._lock = threading.Lock()

    def send(self, text):
        with self._lock:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(text + "\n")


class _AlertState:
    """Tracks one currently-open alert lifecycle for a (camera, rule)
    pair -- from the moment it started matching until it stops."""

    __slots__ = ("rule", "start_time", "detail")

    def __init__(self, rule, start_time, detail=None):
        self.rule = rule
        self.start_time = start_time
        self.detail = detail  # class name for object_class rules, None for motion


def _format_line(event_kind, camera_name, rule_name, trigger_type, detail=None, duration=None):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    trigger_desc = detail if (trigger_type == "object_class" and detail) else "motion"
    if event_kind == "START":
        return f"[{ts}] ALERT START — {camera_name} — \"{rule_name}\" — {trigger_desc}"
    return f"[{ts}] ALERT END   — {camera_name} — \"{rule_name}\" — duration {duration:.0f}s"


def _iso_now():
    return datetime.now().isoformat(timespec="seconds")


class AlertWorker(BackgroundWorker):
    """Background alert lifecycle tracker for a single camera.

    Thread lifecycle comes from BackgroundWorker; this class implements
    tick() and on_stop().
    """

    INTERVAL_SECONDS = TICK_INTERVAL_SECONDS
    LOG_TAG = "alert_manager"

    def __init__(self, cam_id, motion_manager, detection_manager, camera_store, channels,
                 event_store=None, recording_manager=None):
        super().__init__(cam_id)
        self.motion_manager = motion_manager
        self.detection_manager = detection_manager
        self.camera_store = camera_store
        self.channels = channels
        # Phase 7: optional -- both None means "behave exactly like
        # before Phase 7 existed" (log-file channel only, no
        # event_store writes). AlertManager provides both together in
        # practice; see its __init__.
        self.event_store = event_store
        self.recording_manager = recording_manager

        # rule_id -> _AlertState, for whichever rules currently have an
        # open (unfired-END) lifecycle. Motion and object_class rules
        # share this same dict, keyed by rule_id, since rule_ids are
        # unique per camera regardless of trigger_type.
        self._open_states = {}

    def on_stop(self):
        """Close out anything still open rather than leaving a dangling
        START with no matching END in the log -- e.g. the camera was
        removed, or the app is shutting down, mid-alert.

        Safe on the caller's thread: this writes log lines and DB rows,
        it does not release any handle the worker could still be inside.
        """
        for rule_id in list(self._open_states.keys()):
            self._close(rule_id)

    def tick(self):
        camera = self.camera_store.get_camera(self.cam_id)
        if camera is None:
            return
        now_time = datetime.now().time()
        self._tick_motion(camera, now_time)
        self._tick_object_class(camera, now_time)

    # ----- motion rules --------------------------------------------------

    def _tick_motion(self, camera, now_time):
        result = self.motion_manager.get_result(self.cam_id)
        # See module docstring: must OR both halves, or a zoned camera
        # (whole-frame result.motion permanently False) would never
        # fire a motion alert.
        camera_has_motion = result.motion or any(result.zones.values())

        matching_ids = set()
        if camera_has_motion:
            for rule in active_motion_rules(camera, now_time):
                matching_ids.add(rule["id"])
                self._ensure_open(rule, detail=None)

        self._close_stale(trigger_type="motion", still_matching_ids=matching_ids)

    # ----- object-class rules ---------------------------------------------

    def _tick_object_class(self, camera, now_time):
        if not self.camera_store.can_use_object_class_trigger(self.cam_id):
            # Tier chokepoint (always True today, see camera_store.py)
            # -- close out anything that might already be open rather
            # than leaving it stuck if this ever starts returning False
            # for a camera mid-session.
            self._close_stale(trigger_type="object_class", still_matching_ids=set())
            return

        # Multi-instance rework: several classes (or several instances
        # of the same class) can be genuinely present at once now, so
        # this checks every currently-present class against every
        # object_class rule, rather than the old single "most recent
        # event's class" guess. A rule matching more than one present
        # class still only has one open lifecycle (per-rule, not
        # per-instance) -- see the module docstring for why alert
        # rules stay coarser-grained than object_detector.py's own
        # per-instance event firing.
        present_classes = set()
        if self.detection_manager is not None:
            present_classes = self.detection_manager.get_present_classes(self.cam_id)

        matching_ids = set()
        for current_class in present_classes:
            for rule in active_object_class_rules(camera, current_class, now_time):
                matching_ids.add(rule["id"])
                self._ensure_open(rule, detail=current_class)

        self._close_stale(trigger_type="object_class", still_matching_ids=matching_ids)

    # ----- open/close lifecycle + dispatch --------------------------------

    def _ensure_open(self, rule, detail):
        rule_id = rule["id"]
        if rule_id in self._open_states:
            return  # already alerted for this presence -- just keep tracking duration
        state = _AlertState(rule=dict(rule), start_time=time.time(), detail=detail)
        self._open_states[rule_id] = state
        self._dispatch(_format_line(
            "START", self._camera_name(), rule["name"], rule["trigger_type"], detail=detail,
        ))
        self._log_to_event_store("alert_start", rule["name"])

    def _close_stale(self, trigger_type, still_matching_ids):
        for rule_id, state in list(self._open_states.items()):
            if state.rule.get("trigger_type") != trigger_type:
                continue
            if rule_id not in still_matching_ids:
                self._close(rule_id)

    def _close(self, rule_id):
        state = self._open_states.pop(rule_id, None)
        if state is None:
            return
        duration = time.time() - state.start_time
        self._dispatch(_format_line(
            "END", self._camera_name(), state.rule["name"], state.rule["trigger_type"], duration=duration,
        ))
        self._log_to_event_store("alert_end", state.rule["name"], duration=duration)

    def _camera_name(self):
        camera = self.camera_store.get_camera(self.cam_id)
        return camera.get("name", self.cam_id) if camera is not None else self.cam_id

    def _dispatch(self, text):
        for channel in self.channels:
            try:
                channel.send(text)
            except Exception as exc:  # pragma: no cover - defensive
                print(f"[alert_manager] channel send failed: {exc}")

    def _log_to_event_store(self, detection_class, rule_name, duration=None):
        """Phase 7: writes one row into event_store's `events` table
        for this lifecycle edge, alongside the text-line dispatch
        above -- see the module docstring's "Phase 7 addition" section
        for the column-reuse convention (rule_name in zone_id, duration
        in confidence). No-op if this worker wasn't given an
        event_store (e.g. a setup that only cares about the log-file
        channel)."""
        if self.event_store is None:
            return
        segment_id = None
        if self.recording_manager is not None:
            segment_id = self.recording_manager.get_current_segment_id(self.cam_id)
        self.event_store.add_event(
            camera_id=self.cam_id,
            detected_at_iso=_iso_now(),
            detection_class=detection_class,
            zone_id=rule_name,
            confidence=duration,
            segment_id=segment_id,
        )


class AlertManager(WorkerManager):
    """Owns one AlertWorker per camera. Also owns the list of alert
    channels every worker dispatches through -- shared across cameras
    since a channel like LogFileAlertChannel writes to one shared
    file/destination regardless of which camera triggered it."""

    def __init__(self, motion_manager, detection_manager, camera_store, channels=None,
                 event_store=None, recording_manager=None):
        super().__init__()
        self.motion_manager = motion_manager
        self.detection_manager = detection_manager
        self.camera_store = camera_store
        self.channels = channels if channels is not None else [LogFileAlertChannel()]
        # Phase 7: passed straight through to every AlertWorker -- see
        # that class's docstring for why this is a direct call rather
        # than a new AlertChannel implementation.
        self.event_store = event_store
        self.recording_manager = recording_manager

    def _make_worker(self, cam_id, **kwargs):
        return AlertWorker(
            cam_id, self.motion_manager, self.detection_manager, self.camera_store, self.channels,
            event_store=self.event_store, recording_manager=self.recording_manager,
        )

    # ----- back-compat aliases -----------------------------------------

    def start_alerts(self, cam_id):
        return self.start(cam_id)

    def stop_alerts(self, cam_id):
        return self.stop(cam_id)

    def is_alerting(self, cam_id):
        return self.is_active(cam_id)
