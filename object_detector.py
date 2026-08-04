"""
object_detector.py

Phase 4: per-camera object classification, layered on top of Phase 3's
motion detection rather than running independently. Mirrors the
StreamWorker/StreamManager and MotionWorker/MotionManager threading
shape on purpose -- one more background worker per camera, same
lifecycle contract (start/stop/get_status-style access), different job.

Trigger model
-------------
This worker does NOT decode frames or run its own detection loop
against every frame -- it polls MotionManager.get_result(cam_id) on
the same lightweight cadence motion detection itself uses, and only
spends CPU on YOLO inference when there's something worth looking at:

    - "on_motion" mode (default): run inference only when a zone (or,
      for cameras with no zones opted into detection, the whole frame)
      is currently flagged with motion. See camera_store.py's
      object_detection_mode.
    - "continuous" mode: run inference on a fixed per-region cadence
      regardless of motion state. Honors the same zone-priority rule
      "on_motion" mode does -- if the camera has zones with
      detection_enabled, each gets its own cropped, independently
      cooled-down inference call; only a camera with no such zones
      falls back to scanning the whole frame.

Either way, a per-(camera, region) cooldown (DETECTION_COOLDOWN_SECONDS)
caps how often YOLO actually runs against a given zone/whole-frame
region. Lowered to 1s (from an original 10s) as part of the
multi-instance tracking rework below -- responsive enough that a new
arrival is caught within about a second, not up to ten.

Multi-instance presence tracking
---------------------------------
Originally this module kept only the single highest-confidence
detection per region per inference call (`_best_allowed_detection`),
discarding every other qualifying box in the same frame. That meant a
region with a car AND a person only ever reported whichever one YOLO
was more confident about at that instant -- the other was invisible to
the rest of the app, and switched which one "won" moment to moment as
relative confidence shifted, with no relationship to which classes
were actually configured for that camera.

This is replaced with per-(region, class) presence-slot tracking
(`_PresenceSlot`, `_reconcile_slots`). Each slot represents one
instance of a class currently believed present -- a car, say, distinct
from a second car in the same zone. No spatial re-identification
happens between frames (this app doesn't run an object tracker like
SORT/DeepSORT); a slot is a count-based placeholder, not a tracked
physical object. Practically: "at least N instances of this class have
been continuously present, allowing gaps under
ABSENCE_TIMEOUT_SECONDS" is what a slot count represents, not "this
exact car, followed frame to frame." Good enough for "how many, and
for how long" without the complexity of real multi-object tracking.

Each inference call:
    1. Collects EVERY qualifying box, grouped by class
       (_collect_allowed_detections) -- not just the best one.
    2. For each class with either fresh detections this tick or
       existing tracked slots (so a class that just dropped to zero
       still gets a chance to age out), reconciles slot state
       (_reconcile_slots):
       - Slots still "active" (last seen within ABSENCE_TIMEOUT_SECONDS,
         i.e. a re-detection gap under 5s doesn't reset anything) get
         refreshed against however many of this tick's boxes are
         available.
       - Any detected boxes beyond how many slots were already active
         are genuinely NEW instances -- a fresh slot is created for
         each, and each fires a DetectionEvent(is_new=True). This is
         the "send an alert for each additional instance" behavior:
         going from 1 person to 2 fires a second event for the new
         arrival, not a repeat of the first.
       - Slots un-refreshed for more than ABSENCE_TIMEOUT_SECONDS are
         pruned. A class reappearing after that gap has no active
         slots left, so it's correctly treated as a brand new
         presence -- a second alert, per the "goes away for 5s+, alert
         again" requirement.
    3. A continuing (non-new) slot doesn't fire a fresh-detection event
       every single tick just because it keeps being redetected -- but
       it does still periodically fire an is_new=False "still here"
       confirmation, throttled independently
       (STILL_HERE_CONFIRMATION_INTERVAL_SECONDS) from the 1s inference
       cadence, so the live DetectionSidePanel doesn't get flooded with
       a line every second for one person standing still. This mirrors
       the confirmation concept the original design already had, just
       decoupled from the (now much shorter) inference cooldown instead
       of sharing one timer with it.

Per-class, per-camera confidence thresholds
---------------------------------------------
Confidence gating used to be one global constant
(DEFAULT_CONFIDENCE_THRESHOLD, 0.4) applied to every class on every
camera. Now it's per-camera, per-class
(CameraStore.get_class_confidence(cam_id, class_name)) -- e.g. "person"
at 70% on a busy street camera prone to false positives, but "car" at
40% on the same camera since cars are rarely misclassified. A class
with no explicit override falls back to CameraStore.
DEFAULT_CLASS_CONFIDENCE. Configured via Settings -> Object Detection,
one spinbox per checked class.

Cropping
--------
When a zone is the trigger, YOLO doesn't see the whole frame -- it
gets a crop of that zone's axis-aligned bounding box, padded 15% in
each dimension, clamped to the frame's actual bounds. Multiple zones
triggering at once on the same camera each get their own independent
crop + inference call.

Whole-frame fallback (a camera with no zones opted into detection,
relying on Phase 3's whole-frame motion) runs inference on the full
frame, tagged with zone_id=None.

Shared model
------------
One shared `ultralytics.YOLO` instance, loaded lazily, guarded by a
single threading.Lock so inference calls are serialized across
cameras -- see _get_model().

Known limitation: no runtime test environment in the build sandbox --
this has been verified via manual tracing and against the ultralytics
API shape, not against a live camera/model.
"""

import collections
import threading
import time
from ultralytics import YOLO
import cv2
import torch
from motion_detector import _denormalize_polygon  # reuse the same normalized -> pixel helper


class DetectionMode:
    ON_MOTION = "on_motion"
    CONTINUOUS = "continuous"


# How often YOLO actually runs against a given region (zone or whole
# frame). Lowered from an original 10s -- see the module docstring's
# "Multi-instance presence tracking" section -- a 1s cadence is what
# makes new-arrival detection feel responsive without needing to poll
# every single frame.
DETECTION_COOLDOWN_SECONDS = 1.0

# How long a presence slot survives without being re-detected before
# it's considered gone. A gap shorter than this (a single missed
# frame, a brief occlusion) doesn't reset anything -- the same
# presence continues, no new alert. A gap longer than this means the
# next detection of that class is treated as a brand new instance,
# firing a fresh alert.
ABSENCE_TIMEOUT_SECONDS = 5.0

# How often a CONTINUING slot (not a new one) fires an is_new=False
# "still here" confirmation event, independent of the (much shorter)
# DETECTION_COOLDOWN_SECONDS inference cadence -- without this, a
# person standing still would generate a confirmation line every
# single second once inference runs that often, flooding the live
# DetectionSidePanel and the permanent event history. New arrivals are
# NOT subject to this throttle -- every new slot always fires
# immediately, only repeat confirmations of an already-open presence
# are rate-limited.
STILL_HERE_CONFIRMATION_INTERVAL_SECONDS = 10.0

CROP_PADDING_FRACTION = 0.15  # 15% larger in each dimension, centered on the zone's own bounding box
CHECK_INTERVAL_SECONDS = 0.2  # cadence for checking motion state / continuous timing -- matches MotionWorker
MODEL_WEIGHTS = "yolov8n.pt"

# Quality-of-life: a small preview image of what triggered each
# detection, shown as a thumbnail in the side panel's log. Captured
# from the same crop that was actually fed to YOLO (so it shows exactly
# what the model saw), downscaled and JPEG-encoded immediately so the
# bounded in-memory log holds cheap, immutable bytes rather than full
# numpy frames -- safe to pass across threads with no copying concerns.
# One thumbnail is encoded per inference call (not per event) and
# shared across every event that call fires, since they all come from
# the same crop -- see _run_inference.
THUMBNAIL_MAX_DIM = 160
THUMBNAIL_JPEG_QUALITY = 80


def _make_thumbnail(crop_bgr):
    """Downscale + JPEG-encode a detection crop for the side panel's
    thumbnail column. Returns raw JPEG bytes, or None if the crop is
    empty or encoding fails (caller should treat None as "no
    thumbnail available" rather than an error)."""
    if crop_bgr is None or crop_bgr.size == 0:
        return None
    h, w = crop_bgr.shape[:2]
    if max(h, w) > THUMBNAIL_MAX_DIM:
        scale = THUMBNAIL_MAX_DIM / float(max(h, w))
        crop_bgr = cv2.resize(crop_bgr, (max(1, int(w * scale)), max(1, int(h * scale))))
    ok, buf = cv2.imencode(".jpg", crop_bgr, [int(cv2.IMWRITE_JPEG_QUALITY), THUMBNAIL_JPEG_QUALITY])
    if not ok:
        return None
    return buf.tobytes()


class DetectionEvent:
    """One log-worthy outcome of an inference call: either a fresh
    detection (a brand new presence slot) or a "still here"
    confirmation of a continuing one. zone_id/zone_name are None for a
    whole-frame detection. With multi-instance tracking, a single
    inference call can now produce several of these -- one per slot
    that needed firing this tick, not at most one per call."""

    __slots__ = (
        "camera_id", "camera_name", "zone_id", "zone_name",
        "class_name", "confidence", "bbox", "thumbnail", "is_new", "timestamp",
    )

    def __init__(self, camera_id, camera_name, zone_id, zone_name,
                 class_name, confidence, bbox, thumbnail, is_new, timestamp=None):
        self.camera_id = camera_id
        self.camera_name = camera_name
        self.zone_id = zone_id
        self.zone_name = zone_name
        self.class_name = class_name
        self.confidence = confidence
        # (x1, y1, x2, y2) normalized 0.0-1.0 -- fractions of the full
        # frame, same convention zone points use, so the optional
        # bounding-box overlay can reuse VideoLabel's existing
        # letterboxing math instead of needing frame pixel dimensions.
        self.bbox = bbox
        self.thumbnail = thumbnail  # JPEG bytes, or None
        self.is_new = is_new
        self.timestamp = timestamp if timestamp is not None else time.time()

    def message(self):
        where = self.zone_name if self.zone_name else "whole frame"
        ts = time.strftime("%H:%M:%S", time.localtime(self.timestamp))
        pct = f"{self.confidence * 100:.0f}%"
        if self.is_new:
            return f"[{ts}] {self.camera_name}: {self.class_name} detected in {where} ({pct})"
        return f"[{ts}] {self.camera_name}: {self.class_name} still in {where} ({pct})"


_model_lock = threading.Lock()
_shared_model = None
_model_load_failed = False


def _get_model():
    """Lazily load the single shared YOLOv8n model on first use. The
    ultralytics package downloads yolov8n.pt automatically on first
    load if it isn't already cached locally -- that first load needs
    network access. Returns None (and remembers not to retry) if the
    package/model can't be loaded, so callers can skip inference
    quietly instead of crashing worker threads repeatedly."""
    global _shared_model, _model_load_failed
    if _shared_model is not None or _model_load_failed:
        return _shared_model
    with _model_lock:
        if _shared_model is None and not _model_load_failed:
            try:
                _shared_model = YOLO(MODEL_WEIGHTS)
                device = "cuda:0" if torch.cuda.is_available() else "cpu"
                _shared_model.to(device)
                print(f"[object_detector] Using device: {device}")
            except Exception as exc:  # pragma: no cover - defensive
                print(f"[object_detector] Failed to load YOLO model: {exc}")
                _model_load_failed = True
    return _shared_model


def _zone_bbox_pixels(zone, frame_w, frame_h, padding_fraction=CROP_PADDING_FRACTION):
    """Given a zone dict (normalized points) and the frame's actual
    pixel dimensions, return a padded, clamped (x1, y1, x2, y2) integer
    bounding box in that frame's pixel space -- the axis-aligned box
    around the polygon, expanded by padding_fraction in each dimension
    and centered on the polygon's own bounding box (not the frame),
    then clamped so it never runs off the frame's actual edges.
    """
    poly = _denormalize_polygon(zone["points"], frame_w, frame_h).reshape(-1, 2)
    x1, y1 = float(poly[:, 0].min()), float(poly[:, 1].min())
    x2, y2 = float(poly[:, 0].max()), float(poly[:, 1].max())

    w = x2 - x1
    h = y2 - y1
    cx = (x1 + x2) / 2.0
    cy = (y1 + y2) / 2.0

    new_w = w * (1.0 + padding_fraction)
    new_h = h * (1.0 + padding_fraction)

    nx1 = max(0, int(round(cx - new_w / 2.0)))
    ny1 = max(0, int(round(cy - new_h / 2.0)))
    nx2 = min(frame_w, int(round(cx + new_w / 2.0)))
    ny2 = min(frame_h, int(round(cy + new_h / 2.0)))

    return nx1, ny1, nx2, ny2


class _PresenceSlot:
    """One tracked instance of a class currently believed present in a
    region -- see the module docstring's "Multi-instance presence
    tracking" section for the full model. Purely count-based, no
    spatial identity across frames.

    confidence/bbox_norm hold this slot's most recent detection (bbox
    already normalized to full-frame 0.0-1.0 coordinates, same
    convention DetectionEvent.bbox uses) -- updated every tick the
    slot is refreshed. This is what get_active_detections() reads to
    drive the multi-box overlay: every currently active slot's last
    known position, not just the single most recent detection
    app-wide."""

    __slots__ = ("last_seen", "last_confirmed_event_at", "confidence", "bbox_norm")

    def __init__(self, now, confidence, bbox_norm):
        self.last_seen = now
        self.last_confirmed_event_at = now
        self.confidence = confidence
        self.bbox_norm = bbox_norm


class ObjectDetectionWorker:
    """Background object-classification worker for a single camera.
    Mirrors MotionWorker's start/stop lifecycle shape."""

    WHOLE_FRAME_KEY = "__whole_frame__"

    def __init__(self, cam_id, stream_manager, motion_manager, camera_store, on_event=None):
        self.cam_id = cam_id
        self.stream_manager = stream_manager
        self.motion_manager = motion_manager
        self.camera_store = camera_store
        self.on_event = on_event  # callback(DetectionEvent) -- how results reach the log

        self._thread = None
        self._stop_event = threading.Event()

        # (region_key, class_name) -> list[_PresenceSlot]. region_key
        # is a zone_id, or WHOLE_FRAME_KEY for the no-zones-enabled
        # fallback. Protected by _slots_lock since get_present_classes()
        # is read from AlertWorker's thread, not just this worker's own.
        self._slots = {}
        self._slots_lock = threading.Lock()

        # region_key -> last time inference actually ran against it
        # (cooldown tracking).
        self._last_run = {}
        # region_keys considered "motion-active" as of the last tick
        # (on_motion mode only) -- lets a fresh arrival into a region
        # bypass any leftover cooldown instead of waiting out whatever
        # was left from whoever triggered it last.
        self._active_regions = set()

    def start(self):
        if self._thread is not None:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None

    def _run(self):
        while not self._stop_event.is_set():
            try:
                self._tick()
            except Exception as exc:  # pragma: no cover - defensive
                print(f"[object_detector] cam={self.cam_id} tick error: {exc}")
            if self._wait_or_stop(CHECK_INTERVAL_SECONDS):
                return

    def _wait_or_stop(self, seconds):
        return self._stop_event.wait(timeout=seconds)

    def _tick(self):
        camera = self.camera_store.get_camera(self.cam_id)
        if camera is None or not camera.get("object_detection_enabled", False):
            return

        frame = self.stream_manager.get_frame(self.cam_id)
        if frame is None:
            return

        mode = camera.get("object_detection_mode", DetectionMode.ON_MOTION)

        if mode == DetectionMode.CONTINUOUS:
            self._tick_continuous(camera, frame)
        else:
            self._tick_on_motion(camera, frame)

    def _tick_continuous(self, camera, frame):
        """Continuous mode: run on a fixed per-region cadence
        regardless of motion state. Honors the same zone-priority rule
        on_motion mode does: if the camera has zones with
        detection_enabled, each gets its own cropped, independently
        cooled-down inference call; only a camera with no such zones
        falls back to whole-frame scanning."""
        zones = camera.get("zones", [])
        detection_zones = [z for z in zones if z.get("detection_enabled", False)]

        if detection_zones:
            for zone in detection_zones:
                self._maybe_run_zone(camera, frame, zone)
        else:
            self._maybe_run_whole_frame(camera, frame)

    def _tick_on_motion(self, camera, frame):
        motion_result = self.motion_manager.get_result(self.cam_id)
        triggered_zone_ids = [zid for zid, active in motion_result.zones.items() if active]

        if triggered_zone_ids:
            zones_by_id = {z["id"]: z for z in camera.get("zones", [])}
            current_active = set(triggered_zone_ids)
            newly_triggered = current_active - self._active_regions
            for zid in triggered_zone_ids:
                zone = zones_by_id.get(zid)
                if zone is not None:
                    self._maybe_run_zone(camera, frame, zone, force=zid in newly_triggered)
            self._active_regions = current_active
        elif motion_result.motion:
            # Whole-frame fallback -- only reachable when no zone on
            # this camera has detection_enabled (Phase 3's zone-
            # priority rule), same fallback condition MotionWorker uses.
            force = self.WHOLE_FRAME_KEY not in self._active_regions
            self._maybe_run_whole_frame(camera, frame, force=force)
            self._active_regions = {self.WHOLE_FRAME_KEY}
        else:
            # Nothing active anywhere right now. _active_regions is
            # cleared so a later fresh arrival always bypasses any
            # leftover cooldown (see the "force" logic above). Presence
            # slots are NOT explicitly cleared here -- unlike the old
            # single-detection design, slot expiry is computed live
            # from timestamps (now - last_seen), so a slot correctly
            # reads as "gone" once ABSENCE_TIMEOUT_SECONDS has passed
            # even with no inference calls happening to prune it
            # actively; the next real detection naturally starts fresh.
            self._active_regions.clear()

    # ----- gating: cooldown + dispatch to the actual inference call ---

    def _maybe_run_zone(self, camera, frame, zone, force=False):
        zid = zone["id"]
        now = time.time()
        if not force and now - self._last_run.get(zid, 0) < DETECTION_COOLDOWN_SECONDS:
            return
        self._last_run[zid] = now

        frame_h, frame_w = frame.shape[:2]
        x1, y1, x2, y2 = _zone_bbox_pixels(zone, frame_w, frame_h)
        if x2 <= x1 or y2 <= y1:
            return
        crop = frame[y1:y2, x1:x2]
        self._run_inference(
            camera, crop, offset=(x1, y1), region_key=zid, zone=zone,
            frame_w=frame_w, frame_h=frame_h,
        )

    def _maybe_run_whole_frame(self, camera, frame, force=False):
        now = time.time()
        if not force and now - self._last_run.get(self.WHOLE_FRAME_KEY, 0) < DETECTION_COOLDOWN_SECONDS:
            return
        self._last_run[self.WHOLE_FRAME_KEY] = now
        self._run_whole_frame(camera, frame)

    def _run_whole_frame(self, camera, frame):
        frame_h, frame_w = frame.shape[:2]
        self._run_inference(
            camera, frame, offset=(0, 0), region_key=self.WHOLE_FRAME_KEY, zone=None,
            frame_w=frame_w, frame_h=frame_h,
        )

    # ----- actual inference + multi-instance event construction --------

    def _run_inference(self, camera, crop_bgr, offset, region_key, zone, frame_w, frame_h):
        if crop_bgr is None or crop_bgr.size == 0:
            return

        allowed_classes = set(camera.get("object_detection_classes", []) or [])
        if not allowed_classes:
            return  # nothing configured to look for -- skip inference entirely

        model = _get_model()
        if model is None:
            return

        try:
            with _model_lock:
                results = model(crop_bgr, verbose=False)
        except Exception as exc:  # pragma: no cover - defensive
            print(f"[object_detector] inference error on cam={self.cam_id}: {exc}")
            return

        detections_by_class = self._collect_allowed_detections(results, allowed_classes)

        # Normalize every box to full-frame 0.0-1.0 coordinates up
        # front, before reconciliation -- both the fired events AND
        # the persisted slot state (read by get_active_detections for
        # the multi-box overlay) use this same ready-to-draw
        # representation, so there's no separate offset math needed
        # again later.
        ox, oy = offset
        normalized_by_class = {}
        for class_name, boxes in detections_by_class.items():
            normalized = []
            for conf, xyxy in boxes:
                lx1, ly1, lx2, ly2 = xyxy
                px1, py1, px2, py2 = ox + lx1, oy + ly1, ox + lx2, oy + ly2
                bbox_norm = (
                    max(0.0, min(1.0, px1 / frame_w)),
                    max(0.0, min(1.0, py1 / frame_h)),
                    max(0.0, min(1.0, px2 / frame_w)),
                    max(0.0, min(1.0, py2 / frame_h)),
                )
                normalized.append((conf, bbox_norm))
            normalized_by_class[class_name] = normalized

        # Reconcile every class that either has fresh boxes this tick
        # OR already has tracked slots for this region -- the union,
        # not just detected classes, so a class that just dropped to
        # zero detections still gets a chance to age its slots out
        # (see _reconcile_slots).
        with self._slots_lock:
            existing_classes = {cls for (rk, cls) in self._slots.keys() if rk == region_key}
        classes_to_reconcile = existing_classes | set(normalized_by_class.keys())

        now = time.time()
        thumbnail = None  # encoded at most once per call, shared across every event it fires

        for class_name in classes_to_reconcile:
            boxes = normalized_by_class.get(class_name, [])
            fresh_events = self._reconcile_slots(region_key, class_name, boxes, now)

            for is_new, conf, bbox_norm in fresh_events:
                if thumbnail is None:
                    thumbnail = _make_thumbnail(crop_bgr)

                event = DetectionEvent(
                    camera_id=self.cam_id,
                    camera_name=camera.get("name", self.cam_id),
                    zone_id=zone["id"] if zone is not None else None,
                    zone_name=zone.get("name") if zone is not None else None,
                    class_name=class_name,
                    confidence=conf,
                    bbox=bbox_norm,
                    thumbnail=thumbnail,
                    is_new=is_new,
                    timestamp=now,
                )
                if self.on_event is not None:
                    self.on_event(event)

    def _collect_allowed_detections(self, results, allowed_classes):
        """Returns {class_name: [(confidence, xyxy), ...]} for every
        box in this inference call's results whose class is in
        allowed_classes AND whose confidence clears that class's
        camera-specific threshold (camera_store.get_class_confidence)
        -- replaces the old _best_allowed_detection, which kept only
        the single highest-confidence box across all classes combined
        and discarded everything else."""
        by_class = {}
        for r in results:
            boxes = getattr(r, "boxes", None)
            if boxes is None:
                continue
            names = r.names
            for box in boxes:
                cls_idx = int(box.cls[0])
                if isinstance(names, dict):
                    class_name = names.get(cls_idx, str(cls_idx))
                else:
                    class_name = names[cls_idx] if cls_idx < len(names) else str(cls_idx)
                if class_name not in allowed_classes:
                    continue

                conf = float(box.conf[0])
                threshold = self.camera_store.get_class_confidence(self.cam_id, class_name)
                if conf < threshold:
                    continue

                xyxy = tuple(box.xyxy[0].tolist())
                by_class.setdefault(class_name, []).append((conf, xyxy))
        return by_class

    def _reconcile_slots(self, region_key, class_name, boxes, now):
        """Updates the tracked presence slots for one (region, class)
        pair against this tick's detected boxes (a list of
        (confidence, bbox_norm) tuples, already normalized to
        full-frame 0.0-1.0 coordinates), and returns a list of
        (is_new, confidence, bbox_norm) tuples for whichever slots need
        a DetectionEvent fired this tick. See the module docstring's
        "Multi-instance presence tracking" section for the full
        reasoning."""
        key = (region_key, class_name)
        events = []

        with self._slots_lock:
            slots = self._slots.get(key, [])

            active_slots = [s for s in slots if now - s.last_seen <= ABSENCE_TIMEOUT_SECONDS]
            detected_count = len(boxes)

            # Refresh however many currently-active slots this tick's
            # detections cover -- these are "still here," not new.
            # Their stored confidence/bbox_norm are updated too, so
            # get_active_detections() always reflects each slot's most
            # recent known position for the overlay.
            to_refresh = min(len(active_slots), detected_count)
            for i in range(to_refresh):
                slot = active_slots[i]
                conf, bbox_norm = boxes[i]
                slot.last_seen = now
                slot.confidence = conf
                slot.bbox_norm = bbox_norm
                if now - slot.last_confirmed_event_at >= STILL_HERE_CONFIRMATION_INTERVAL_SECONDS:
                    slot.last_confirmed_event_at = now
                    events.append((False, conf, bbox_norm))

            # Any boxes left over beyond how many slots were already
            # active are genuinely new instances -- one fresh slot (and
            # one is_new=True event) per extra box.
            for conf, bbox_norm in boxes[to_refresh:]:
                new_slot = _PresenceSlot(now, conf, bbox_norm)
                slots.append(new_slot)
                events.append((True, conf, bbox_norm))

            # Prune anything un-refreshed past ABSENCE_TIMEOUT_SECONDS
            # -- including slots that were already stale coming into
            # this tick. A later re-detection with nothing left active
            # is correctly treated as a brand new presence.
            slots = [s for s in slots if now - s.last_seen <= ABSENCE_TIMEOUT_SECONDS]
            if slots:
                self._slots[key] = slots
            else:
                self._slots.pop(key, None)

        return events

    def get_present_classes(self):
        """Class names with at least one currently-active presence
        slot, across every region on this camera. This is what
        AlertWorker's object_class rule matching uses (via
        ObjectDetectionManager.get_present_classes) instead of the old
        approach of guessing "currently present" from the recency of a
        single latest event -- this worker now maintains real presence
        state, so alert_manager.py can just ask it directly."""
        now = time.time()
        present = set()
        with self._slots_lock:
            for (_region_key, class_name), slots in self._slots.items():
                if any(now - s.last_seen <= ABSENCE_TIMEOUT_SECONDS for s in slots):
                    present.add(class_name)
        return present

    def get_active_detections(self):
        """Every currently-active presence slot's most recent
        (class_name, confidence, bbox_norm), across all regions on
        this camera -- what the bounding-box overlay draws now that it
        shows every concurrently present instance instead of only the
        single most recent detection app-wide. bbox_norm reflects
        whatever tick last refreshed that slot, so a slot that hasn't
        been re-detected in the last second or two (but is still
        within ABSENCE_TIMEOUT_SECONDS) shows its last known position,
        not a live-tracked one -- there's no per-frame tracking here,
        just per-inference-tick presence (see the module docstring)."""
        now = time.time()
        active = []
        with self._slots_lock:
            for (_region_key, class_name), slots in self._slots.items():
                for slot in slots:
                    if now - slot.last_seen <= ABSENCE_TIMEOUT_SECONDS:
                        active.append((class_name, slot.confidence, slot.bbox_norm))
        return active


class ObjectDetectionManager:
    """Owns one ObjectDetectionWorker per camera, mirrors
    StreamManager/MotionManager's shape. Also owns the shared, bounded,
    in-memory detection log every worker's events feed into -- Phase 4
    has no persistence yet (Phase 7's SQLite events table adds that);
    this exists purely to feed the live "Detections" dock panel."""

    LOG_MAXLEN = 200

    def __init__(self, stream_manager, motion_manager, camera_store):
        self.stream_manager = stream_manager
        self.motion_manager = motion_manager
        self.camera_store = camera_store
        self._workers = {}  # cam_id -> ObjectDetectionWorker

        self._log = collections.deque(maxlen=self.LOG_MAXLEN)
        self._total_appended = 0
        self._log_lock = threading.Lock()
        # cam_id -> most recent DetectionEvent, for the caption-bar
        # detection badge (a compact "something was recently seen"
        # indicator -- deliberately still single-detection, see
        # get_latest_event's docstring). Kept separate from the log's
        # history -- the badge only ever cares about "right now,"
        # never past events.
        self._latest_by_camera = {}

    def start_detection(self, cam_id):
        self.stop_detection(cam_id)
        worker = ObjectDetectionWorker(
            cam_id, self.stream_manager, self.motion_manager, self.camera_store,
            on_event=self._on_event,
        )
        worker.start()
        self._workers[cam_id] = worker

    def stop_detection(self, cam_id):
        worker = self._workers.pop(cam_id, None)
        if worker is not None:
            worker.stop()

    def stop_all(self):
        for cam_id in list(self._workers.keys()):
            self.stop_detection(cam_id)

    def is_detecting(self, cam_id):
        return cam_id in self._workers

    def active_camera_ids(self):
        return list(self._workers.keys())

    def _on_event(self, event):
        # Called from a worker's background thread -- lock around the
        # shared deque/dict since the GUI thread reads them concurrently.
        with self._log_lock:
            self._log.append(event)
            self._total_appended += 1
            self._latest_by_camera[event.camera_id] = event

    def get_latest_event(self, cam_id):
        """Most recent DetectionEvent for a camera, or None. Used by
        the caption-bar detection badge (a compact single-detection
        indicator) -- NOT used by the bounding-box overlay anymore
        (see get_active_detections for that) or the log panel, which
        reads history via get_new_events instead."""
        with self._log_lock:
            return self._latest_by_camera.get(cam_id)

    def get_new_events(self, since_count):
        """Returns (events, new_since_count). Pass the count this
        method last returned; get back only events appended since
        then, oldest first. If the bounded log has evicted events
        you hadn't seen yet (a long gap between polls), you just get
        whatever's still available -- there's no persistence to fall
        back on in Phase 4."""
        with self._log_lock:
            total = self._total_appended
            dropped = total - len(self._log)
            since_count = max(since_count, dropped)
            skip = since_count - dropped
            events = list(self._log)[skip:]
        return events, total

    def get_present_classes(self, cam_id):
        """Class names currently believed present on this camera
        (across all its regions) -- see
        ObjectDetectionWorker.get_present_classes. Used by
        alert_manager.py for object_class rule matching. Empty set if
        this camera has no active detection worker."""
        worker = self._workers.get(cam_id)
        if worker is None:
            return set()
        return worker.get_present_classes()

    def get_active_detections(self, cam_id):
        """Every currently-active detection (class_name, confidence,
        bbox_norm) on this camera, across all its regions -- see
        ObjectDetectionWorker.get_active_detections. Drives the
        multi-box overlay in main.py (GridView/SingleView). Empty list
        if this camera has no active detection worker."""
        worker = self._workers.get(cam_id)
        if worker is None:
            return []
        return worker.get_active_detections()