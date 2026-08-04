"""
motion_detector.py

Phase 3: per-camera motion detection, running as its own background
thread per camera (mirrors video_stream.py's StreamWorker/StreamManager
pattern deliberately, so the two systems read the same way).

Pipeline per camera, per processed frame:
    0. Check the camera's master motion_enabled switch -- if off, skip
       everything below (no MOG2, no CPU cost). This was originally
       documented but not actually implemented; see the "bug fix" note
       in MotionWorker._process_frame below.
    1. Pull the latest decoded frame from StreamManager.get_frame(cam_id)
       (we don't touch video_stream.py at all -- this worker is just
       another reader, the same way the GUI's poll loop is).
    2. Run cv2.createBackgroundSubtractorMOG2() to get a foreground mask.
    3. Threshold + count changed pixels for "whole frame motion".
    4. If the camera has zones, also AND the foreground mask against
       each zone's polygon mask and count changed pixels *inside* that
       zone only -- this is the "zone-restricted" half of Phase 3.

Coordinate space note (the thing the roadmap flagged as the tricky
part): zone_editor.py's docstring confirms zone points are normalized
0.0-1.0 against the *source frame's* width/height, NOT against the
fixed 1280x720 editing canvas (that canvas size is purely an on-screen
drawing surface -- "we only convert to/from normalized space at the
storage boundary"). That means there is no special reconciliation step
here: denormalizing a zone's points just means multiplying by the
*actual decoded frame's* (width, height) at the moment we build the
mask. The 1280x720 editor scene size is never used in this file.

Mask caching: cv2.fillPoly to rebuild a zone's mask from scratch every
single frame is wasted work when the zone polygon hasn't changed.
Masks are cached per (zone signature, frame size) and only rebuilt when
invalidate_zones() is called (hook for MainWindow.notify_zones_changed)
or when the incoming frame's resolution changes (e.g. camera
reconnects at a different resolution).

Sensitivity: motion false-positive rates vary a lot scene-to-scene
(lighting changes, trees, IR noise at night), so the roadmap calls for
this to be tunable per camera rather than a single hardcoded constant.
Stored back on the camera dict as "motion_threshold" (changed-pixel
count required to call it motion) -- absent on cameras created before
Phase 3, in which case DEFAULT_MOTION_THRESHOLD is used, the same
missing-key-defaults-gracefully pattern camera_store.py already uses
for "zones".
"""

import threading
import time

import cv2
import numpy as np


class MotionStatus:
    STARTING = "starting"     # subtractor still warming up its background model
    RUNNING = "running"
    NO_FRAME = "no_frame"     # camera has no decoded frame yet (still connecting)
    DISABLED = "disabled"     # camera's motion_enabled master switch is off
    STOPPED = "stopped"


# Changed-pixel count (not percentage -- simpler to reason about, and
# resolution-independence is already handled by zones being normalized;
# whole-frame counts naturally scale with resolution, which is fine
# since this is a per-camera tunable, not a cross-camera comparison).
DEFAULT_MOTION_THRESHOLD = 500

# MOG2 needs a handful of frames to build an initial background model
# before its foreground mask is meaningful -- before that, it tends to
# flag the entire frame as "foreground" since everything is new to it.
# Suppress motion=True during this warmup window rather than reporting
# a guaranteed-spurious initial detection.
WARMUP_FRAMES = 30

# How often this worker pulls a new frame and runs detection. Doesn't
# need to match the GUI's 30ms poll -- motion detection on every single
# decoded frame is unnecessary work; a lighter cadence is plenty for
# "did something move" while keeping CPU load down across many cameras.
DETECT_INTERVAL_SECONDS = 0.2


def _denormalize_polygon(points, frame_w, frame_h):
    """points: list of [norm_x, norm_y] (0.0-1.0, as stored in
    camera_store.py). Returns an (N, 1, 2) int32 numpy array of actual
    pixel coordinates in *this frame's* space, ready for cv2.fillPoly.

    This is the one place normalized -> pixel-space conversion happens
    for masking purposes. Deliberately uses the frame's own width/
    height, not zone_editor.py's FRAME_SCENE_W/H -- see module
    docstring."""
    pts = np.array(
        [[x * frame_w, y * frame_h] for x, y in points],
        dtype=np.int32,
    )
    return pts.reshape((-1, 1, 2))


def _zone_signature(zones):
    """Cheap hashable fingerprint of a zone list's shape (ids + point
    values), so the mask cache can tell "zones changed" from "zones
    are the same as last time" without deep-comparing dicts on every
    frame. Order-sensitive, which is fine -- we rebuild the whole cache
    on any mismatch anyway."""
    return tuple(
        (z["id"], tuple(tuple(p) for p in z["points"]))
        for z in zones
    )


class MotionResult:
    """Snapshot of the latest detection outcome for one camera.

    - motion: whole-frame motion bool (True if changed_pixels exceeds
      this camera's threshold)
    - changed_pixels: whole-frame foreground pixel count
    - zones: {zone_id: bool} -- per-zone motion bool, restricted to
      that zone's polygon mask. Empty dict if the camera has no zones.
    - zone_changed_pixels: {zone_id: int} -- per-zone foreground pixel
      count, for anyone tuning sensitivity or building a debug view
      later (Phase 4/5 may want this rather than just the bool).
    """

    __slots__ = ("motion", "changed_pixels", "zones", "zone_changed_pixels", "timestamp")

    def __init__(self, motion=False, changed_pixels=0, zones=None, zone_changed_pixels=None, timestamp=None):
        self.motion = motion
        self.changed_pixels = changed_pixels
        self.zones = zones or {}
        self.zone_changed_pixels = zone_changed_pixels or {}
        self.timestamp = timestamp if timestamp is not None else time.time()


class MotionWorker:
    """Background motion detector for a single camera. Mirrors
    StreamWorker's start/stop/get_* shape on purpose -- same lifecycle
    pattern, different job."""

    def __init__(self, cam_id, stream_manager, camera_store):
        self.cam_id = cam_id
        self.stream_manager = stream_manager
        self.camera_store = camera_store

        self._subtractor = cv2.createBackgroundSubtractorMOG2(
            detectShadows=False,  # shadow detection just adds a third
                                  # "gray" pixel class we'd have to
                                  # filter back out before thresholding;
                                  # not worth it for a binary motion flag
        )
        self._frames_seen = 0
        # Tracks whether the previous processed frame was skipped due
        # to motion_enabled being off, so the next enabled frame knows
        # to rebuild the subtractor rather than resume with a
        # background model that may now be stale relative to the real
        # scene -- see _process_frame's bug-fix note below.
        self._was_disabled = False

        self._thread = None
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._zones_dirty = threading.Event()  # set -> rebuild mask cache next frame
        self._zones_dirty.set()  # build it fresh on first frame too

        self._status = MotionStatus.STARTING
        self._latest_result = MotionResult()

        # Mask cache: (zone_signature, frame_w, frame_h) -> {zone_id: mask}
        self._mask_cache_key = None
        self._mask_cache = {}

    # ----- public API (mirrors StreamWorker) --------------------------

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
        with self._lock:
            self._status = MotionStatus.STOPPED

    def get_result(self):
        with self._lock:
            return self._latest_result

    def get_status(self):
        with self._lock:
            return self._status

    def invalidate_zones(self):
        """Call when this camera's zones changed (add/edit/remove).
        Doesn't rebuild anything itself -- just flags the cache as
        stale so the next processed frame rebuilds it. Cheap and safe
        to call from any thread (MainWindow.notify_zones_changed runs
        on the GUI thread; this worker reads the flag on its own
        thread)."""
        self._zones_dirty.set()

    # ----- internals ---------------------------------------------------

    def _run(self):
        while not self._stop_event.is_set():
            frame = self.stream_manager.get_frame(self.cam_id)

            if frame is None:
                with self._lock:
                    self._status = MotionStatus.NO_FRAME
                if self._wait_or_stop(DETECT_INTERVAL_SECONDS):
                    return
                continue

            self._process_frame(frame)

            if self._wait_or_stop(DETECT_INTERVAL_SECONDS):
                return

    def _process_frame(self, frame_bgr):
        camera = self.camera_store.get_camera(self.cam_id)
        enabled = camera.get("motion_enabled", True) if camera is not None else True

        if not enabled:
            # Bug fix: this master switch was documented ("if off,
            # skip everything -- no MOG2, no CPU cost") but never
            # actually checked here -- the pipeline ran regardless,
            # and the switch only affected the Settings UI's dimming
            # state. Now it actually gates the work.
            self._was_disabled = True
            with self._lock:
                self._status = MotionStatus.DISABLED
                self._latest_result = MotionResult()
            return

        if self._was_disabled:
            # Coming back from being disabled: the subtractor's
            # background model may reflect a stale scene from before
            # the pause (lighting changed, something moved into frame
            # and stayed, etc.), and _frames_seen already looks
            # "warmed up" even though that model's memory of the scene
            # might not be accurate anymore. Rebuild both so re-
            # enabling always re-warms cleanly, exactly like a freshly
            # started worker would.
            self._subtractor = cv2.createBackgroundSubtractorMOG2(detectShadows=False)
            self._frames_seen = 0
            self._was_disabled = False

        frame_h, frame_w = frame_bgr.shape[:2]

        gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray, (5, 5), 0)

        fg_mask = self._subtractor.apply(gray)
        # apply() returns 0/255 (or 0/127/255 with shadow detection,
        # but that's disabled above) -- threshold defensively anyway in
        # case OpenCV's output convention differs across versions.
        _, fg_mask = cv2.threshold(fg_mask, 200, 255, cv2.THRESH_BINARY)

        self._frames_seen += 1
        warming_up = self._frames_seen <= WARMUP_FRAMES

        whole_frame_changed = int(cv2.countNonZero(fg_mask))

        threshold = camera.get("motion_threshold", DEFAULT_MOTION_THRESHOLD)
        zones = camera.get("zones", [])

        zone_masks = self._get_zone_masks(zones, frame_w, frame_h)

        zone_bools = {}
        zone_pixel_counts = {}
        for zone_id, mask in zone_masks.items():
            masked = cv2.bitwise_and(fg_mask, fg_mask, mask=mask)
            count = int(cv2.countNonZero(masked))
            zone_pixel_counts[zone_id] = count
            zone_bools[zone_id] = (not warming_up) and (count >= threshold)

        result = MotionResult(
            motion=(not warming_up) and (whole_frame_changed >= threshold),
            changed_pixels=whole_frame_changed,
            zones=zone_bools,
            zone_changed_pixels=zone_pixel_counts,
        )

        with self._lock:
            self._latest_result = result
            self._status = MotionStatus.STARTING if warming_up else MotionStatus.RUNNING

    def _get_zone_masks(self, zones, frame_w, frame_h):
        """Returns {zone_id: uint8 mask} for the given zones at the
        given frame size, rebuilding from the cache only when the zone
        list or frame size has actually changed since last time."""
        cache_key = (_zone_signature(zones), frame_w, frame_h)

        if not self._zones_dirty.is_set() and cache_key == self._mask_cache_key:
            return self._mask_cache

        self._mask_cache = {}
        for zone in zones:
            points = zone.get("points", [])
            if len(points) < 3:
                continue  # malformed/degenerate zone -- skip rather than crash
            mask = np.zeros((frame_h, frame_w), dtype=np.uint8)
            poly = _denormalize_polygon(points, frame_w, frame_h)
            cv2.fillPoly(mask, [poly], 255)
            self._mask_cache[zone["id"]] = mask

        self._mask_cache_key = cache_key
        self._zones_dirty.clear()
        return self._mask_cache

    def _wait_or_stop(self, seconds):
        return self._stop_event.wait(timeout=seconds)


class MotionManager:
    """Owns one MotionWorker per camera with detection enabled, mirrors
    StreamManager's shape. Kept separate from StreamManager rather than
    merged into it -- motion detection is an optional, independently
    start/stoppable layer on top of streaming, not a part of streaming
    itself (a camera can be viewed live with detection off)."""

    def __init__(self, stream_manager, camera_store):
        self.stream_manager = stream_manager
        self.camera_store = camera_store
        self._workers = {}  # cam_id -> MotionWorker

    def start_detection(self, cam_id):
        self.stop_detection(cam_id)  # no duplicate worker for this id
        worker = MotionWorker(cam_id, self.stream_manager, self.camera_store)
        worker.start()
        self._workers[cam_id] = worker

    def stop_detection(self, cam_id):
        worker = self._workers.pop(cam_id, None)
        if worker is not None:
            worker.stop()

    def stop_all(self):
        for cam_id in list(self._workers.keys()):
            self.stop_detection(cam_id)

    def get_result(self, cam_id):
        worker = self._workers.get(cam_id)
        if worker is None:
            return MotionResult()
        return worker.get_result()

    def get_status(self, cam_id):
        worker = self._workers.get(cam_id)
        if worker is None:
            return MotionStatus.STOPPED
        return worker.get_status()

    def is_detecting(self, cam_id):
        return cam_id in self._workers

    def notify_zones_changed(self, cam_id):
        """Call this from MainWindow.notify_zones_changed alongside the
        existing live-view sync, so a zone added/edited/removed while
        detection is running invalidates that camera's mask cache
        immediately rather than waiting for a frame-size mismatch to
        force a rebuild (which would only happen by coincidence)."""
        worker = self._workers.get(cam_id)
        if worker is not None:
            worker.invalidate_zones()

    def active_camera_ids(self):
        return list(self._workers.keys())