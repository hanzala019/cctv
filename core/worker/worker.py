"""
core.worker.worker

Base class for the per-camera background workers.

StreamWorker, MotionWorker, ObjectDetectionWorker, AlertWorker,
EventLoggerWorker and RecordingWorker all implemented the same
start/stop/_wait_or_stop/loop-with-try-except lifecycle independently,
with small unintentional differences (different join timeouts, some
loops catching exceptions and some not, some setting a STOPPED status
and some not). This is that lifecycle, once.

Subclasses implement `tick()` -- one pass of whatever the worker does
-- and optionally override `on_stop()` for cleanup that must run on the
caller's thread after the loop has exited.
"""

import threading


class BackgroundWorker:
    """One background thread doing periodic work for a single camera."""

    #: Seconds to wait between tick() calls.
    #:
    #: Set to 0 for workers whose tick() is itself blocking and paces
    #: the loop (a stream reader blocking in cap.read()). Any non-zero
    #: value there would throttle decode below the stream's frame rate
    #: and the worker would fall progressively behind the buffer.
    INTERVAL_SECONDS = 0.2

    #: how long stop() waits for the thread to exit before giving up
    JOIN_TIMEOUT_SECONDS = 2.0

    #: prefix for log lines, so failures say which subsystem broke
    LOG_TAG = "worker"

    def __init__(self, cam_id):
        self.cam_id = cam_id
        self._thread = None
        self._stop_event = threading.Event()

    # ----- lifecycle --------------------------------------------------

    def start(self):
        if self._thread is not None:
            return  # already running
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, daemon=True,
            name=f"{self.LOG_TAG}-{self.cam_id}",
        )
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=self.JOIN_TIMEOUT_SECONDS)
            if thread.is_alive():
                # Blocked in a syscall (a dead RTSP read, a slow disk
                # write). Say so rather than continuing silently and
                # tearing down resources the thread may still be using.
                print(
                    f"[{self.LOG_TAG}] cam={self.cam_id} thread did not exit "
                    f"within {self.JOIN_TIMEOUT_SECONDS}s"
                )
        self.on_stop()

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    # ----- to implement in subclasses -----------------------------------

    def tick(self):
        """One pass of this worker's job. Called every INTERVAL_SECONDS
        until stop() is requested. Exceptions are caught and logged, so
        a transient failure doesn't silently kill the thread."""
        raise NotImplementedError

    def on_thread_exit(self):
        """Cleanup that MUST run on the worker's own thread, as the loop
        exits. Release capture handles, writers and sockets here.

        This exists because the alternative -- releasing from stop() on
        the caller's thread -- is a genuine crash. If the worker is
        blocked inside a C call (cv2.VideoCapture.read on a dead RTSP
        socket) and the caller frees the handle underneath it, that is a
        segfault, not an exception.

        Caveat: if the thread is wedged and stop()'s join times out,
        this never runs and the handle leaks. A leak is strictly better
        than a crash, and stop() logs the timeout so it is visible.
        """

    def on_stop(self):
        """Cleanup after stop() has joined the thread. Runs on the
        CALLER's thread, so keep it to things that are safe there --
        flipping a lock-protected status flag, closing out a DB row.
        Never release a handle the worker might still be inside; use
        on_thread_exit() for that."""

    # ----- internals ------------------------------------------------------

    def _run(self):
        try:
            while not self._stop_event.is_set():
                try:
                    self.tick()
                except Exception as exc:  # noqa: BLE001 -- see GUIDELINE.md 7
                    # Deliberately broad: this is the worker top-level
                    # wrapper, one of the two places a bare Exception is
                    # allowed. A transient camera or disk failure must
                    # not silently end the thread. Always logged.
                    print(f"[{self.LOG_TAG}] cam={self.cam_id} tick error: {exc}")
                if self._wait_or_stop(self.INTERVAL_SECONDS):
                    return
        finally:
            # try/finally so the handle is released even if tick() raises
            # something the loop doesn't catch, or the thread is exiting
            # via return.
            try:
                self.on_thread_exit()
            except Exception as exc:  # noqa: BLE001 -- see GUIDELINE.md 7
                # Cleanup must never mask the reason the thread exited.
                print(f"[{self.LOG_TAG}] cam={self.cam_id} cleanup error: {exc}")

    def _wait_or_stop(self, seconds):
        """Sleep up to `seconds`, returning True early if stop() was
        called -- so shutdown is immediate rather than waiting out a
        full interval per worker."""
        return self._stop_event.wait(timeout=seconds)
