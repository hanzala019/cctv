"""
event_logger.py

Phase 7: logs motion start/stop lifecycle edges into event_store's
`events` table. EventLoggerWorker mirrors AlertWorker's shape almost
exactly (same camera-wide "motion or any zone" OR rule, same open/
close-state tracking) but deliberately does NOT share AlertWorker's
state or reuse alert_manager.py's code -- this is unconditional (logs
every motion presence regardless of whether any alert rule's time
window matches it, since it's building a complete local record, not
deciding whether to notify anyone), so folding it into AlertWorker
would tangle two different concerns -- rule-gated notification vs.
unconditional history -- that happen to compute a similar boolean.

Two rows per motion presence (`motion_start` / `motion_end`) rather
than one, per the Phase 7 design discussion -- the events table has no
duration column, so a start/end pair is how a later query can derive
how long a presence lasted (the same information alert_manager.py's
log line embeds directly via its own duration calculation, just spread
across two rows here instead of computed once and written into one log
line).

Object-detection events are NOT logged by this worker -- those are
already discrete (one DetectionEvent per classified hit), so
MainWindow logs them directly to event_store from the same poll-loop
pass that already pulls new events for the DetectionSidePanel, rather
than adding a second worker/thread whose only job would be turning an
already-discrete event stream into... the same discrete events again.
"""

import threading
from datetime import datetime

CHECK_INTERVAL_SECONDS = 0.5  # matches alert_manager.py's tick cadence -- not latency-sensitive


def _iso_now():
    return datetime.now().isoformat(timespec="seconds")


class EventLoggerWorker:
    """Background motion-lifecycle logger for a single camera."""

    def __init__(self, cam_id, motion_manager, recording_manager, event_store):
        self.cam_id = cam_id
        self.motion_manager = motion_manager
        self.recording_manager = recording_manager
        self.event_store = event_store

        self._thread = None
        self._stop_event = threading.Event()
        self._motion_open = False  # True while a motion_start has fired with no matching motion_end yet

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
        # Close out a dangling presence the same way AlertWorker.stop()
        # does, so removing a camera or shutting down mid-motion doesn't
        # leave a motion_start with no matching motion_end in the log.
        if self._motion_open:
            self._log("motion_end")
            self._motion_open = False

    def _run(self):
        while not self._stop_event.is_set():
            try:
                self._tick()
            except Exception as exc:  # pragma: no cover - defensive
                print(f"[event_logger] cam={self.cam_id} tick error: {exc}")
            if self._stop_event.wait(timeout=CHECK_INTERVAL_SECONDS):
                return

    def _tick(self):
        result = self.motion_manager.get_result(self.cam_id)
        # Same OR-both-halves rule alert_manager.py uses -- once any
        # zone has detection_enabled, the whole-frame bool is
        # permanently False (Phase 3's zone-priority rule), so a
        # zoned camera needs the zones half checked too.
        has_motion = result.motion or any(result.zones.values())

        if has_motion and not self._motion_open:
            self._log("motion_start")
            self._motion_open = True
        elif not has_motion and self._motion_open:
            self._log("motion_end")
            self._motion_open = False

    def _log(self, detection_class):
        segment_id = self.recording_manager.get_current_segment_id(self.cam_id)
        self.event_store.add_event(
            camera_id=self.cam_id,
            detected_at_iso=_iso_now(),
            detection_class=detection_class,
            segment_id=segment_id,
        )


class EventLoggerManager:
    """Owns one EventLoggerWorker per camera, mirrors every other
    *Manager's shape in this app."""

    def __init__(self, motion_manager, recording_manager, event_store):
        self.motion_manager = motion_manager
        self.recording_manager = recording_manager
        self.event_store = event_store
        self._workers = {}

    def start_logging(self, cam_id):
        self.stop_logging(cam_id)
        worker = EventLoggerWorker(cam_id, self.motion_manager, self.recording_manager, self.event_store)
        worker.start()
        self._workers[cam_id] = worker

    def stop_logging(self, cam_id):
        worker = self._workers.pop(cam_id, None)
        if worker is not None:
            worker.stop()

    def stop_all(self):
        for cam_id in list(self._workers.keys()):
            self.stop_logging(cam_id)

    def active_camera_ids(self):
        return list(self._workers.keys())