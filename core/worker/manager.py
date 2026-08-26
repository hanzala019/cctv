"""
core.worker.manager

Base class for the per-camera worker registries.

StreamManager, MotionManager, ObjectDetectionManager, AlertManager,
EventLoggerManager and RecordingManager were six copies of the same
thirty lines: a `_workers` dict keyed by camera id, a start that
stop()s any existing worker first, a stop that pops and stops, a
stop_all, and an active_camera_ids. This is that, once.

Subclasses implement `_make_worker(cam_id)` and keep whatever
domain-specific accessors they need (get_frame, get_result,
get_current_segment_id, ...).

Note on method names: each subclass previously exposed a differently
named start/stop pair (start_stream, start_detection, start_alerts,
start_recording, start_logging). Those are kept as thin aliases on the
subclasses so existing call sites in ui/app.py keep working, but new
code should call start()/stop().
"""


class WorkerManager:
    """Owns at most one worker per camera id."""

    def __init__(self):
        self._workers = {}

    # ----- to implement in subclasses ------------------------------------

    def _make_worker(self, cam_id, **kwargs):
        raise NotImplementedError

    # ----- lifecycle -------------------------------------------------------

    def start(self, cam_id, **kwargs):
        self.stop(cam_id)  # never leave a duplicate worker for this id
        worker = self._make_worker(cam_id, **kwargs)
        worker.start()
        self._workers[cam_id] = worker
        return worker

    def stop(self, cam_id):
        worker = self._workers.pop(cam_id, None)
        if worker is not None:
            worker.stop()

    def stop_all(self):
        for cam_id in list(self._workers):
            self.stop(cam_id)

    # ----- queries ---------------------------------------------------------

    def get(self, cam_id):
        return self._workers.get(cam_id)

    def is_active(self, cam_id):
        return cam_id in self._workers

    def active_camera_ids(self):
        return list(self._workers)

    def __contains__(self, cam_id):
        return cam_id in self._workers

    def __len__(self):
        return len(self._workers)
