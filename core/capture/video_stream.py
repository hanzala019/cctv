"""
core.capture.video_stream

Wraps cv2.VideoCapture in a background thread so reading frames from a
network stream (RTSP/TCP/UDP/HTTP) never blocks the PyQt6 event loop.

Each StreamWorker continuously grabs the latest frame and stores it.
The GUI polls `get_frame()` on its own schedule (every POLL_INTERVAL_MS)
instead of being driven by the network -- so a slow or dead camera
produces a stale or blank tile, not a frozen app.

Threading shape
---------------
StreamWorker is a BackgroundWorker with INTERVAL_SECONDS = 0. Unlike the
polling workers, its tick() blocks inside cap.read(), and the stream's
own frame rate paces the loop. A sleep here would throttle decode below
the camera's frame rate and fall progressively behind the buffer.

The capture handle is opened and released on the worker's own thread
(see on_thread_exit). Releasing it from stop() on the caller's thread --
which is what this module used to do after a timed-out join -- can free
the handle while FFmpeg is still inside cap.read(). That segfaults the
process rather than raising.
"""

import threading

import cv2

from core.worker.manager import WorkerManager
from core.worker.worker import BackgroundWorker


class StreamStatus:
    CONNECTING = "connecting"
    CONNECTED = "connected"
    ERROR = "error"
    STOPPED = "stopped"


class StreamWorker(BackgroundWorker):
    """Background reader for a single camera URL."""

    # tick() blocks in cap.read(); the stream paces the loop.
    INTERVAL_SECONDS = 0
    LOG_TAG = "stream"

    # How long to wait before considering a connection attempt failed.
    # Without this, a dead RTSP/TCP target makes OpenCV/FFmpeg block for
    # ~30s by default before giving up.
    OPEN_TIMEOUT_MSEC = 5000
    READ_TIMEOUT_MSEC = 5000
    # Delay before retrying after a failed/dropped connection.
    RETRY_DELAY_SECONDS = 3

    # Treat the stream as dropped after this many consecutive bad reads.
    MAX_CONSECUTIVE_FAILURES = 20
    # Don't get stuck rejecting corrupt frames forever -- force a
    # reconnect and let the decoder start clean.
    MAX_CONSECUTIVE_CORRUPT = 10

    CORRUPTION_DIFF_THRESHOLD = 70.0
    CORRUPTION_THUMB_SIZE = (64, 64)

    def __init__(self, cam_id, url):
        super().__init__(cam_id)
        self.url = url

        self._cap = None

        # Guards frame/status state only. BackgroundWorker owns the
        # thread and stop event; it does not provide a lock, because
        # most workers have nothing to guard.
        self._lock = threading.Lock()

        self._latest_frame = None  # most recent BGR frame (numpy array) or None
        # Incremented every time a new decoded frame is stored. Starts at
        # 0 ("no frame yet") so any real frame (counter >= 1) is
        # distinguishable from "nothing decoded yet".
        self._frame_counter = 0
        self._status = StreamStatus.CONNECTING
        self._error_message = None

        # Read-loop state. Lives on the instance rather than as locals in
        # a while loop, because tick() is now called once per frame
        # instead of owning the loop itself.
        self._consecutive_failures = 0
        self._consecutive_corrupt = 0
        # Cached 64x64 downsample of the last accepted frame, so the
        # corruption check resizes one frame per iteration, not two.
        self._last_good_small = None

    # ----- public API -----------------------------------------------

    def get_frame(self):
        """Returns the most recent frame (numpy BGR array) or None."""
        with self._lock:
            return self._latest_frame

    def get_frame_counter(self):
        """Monotonic count of frames decoded so far (0 = none yet).
        Callers compare this against a previously-seen value to tell
        whether get_frame() would return a genuinely new frame, without
        having to diff the frame buffers themselves."""
        with self._lock:
            return self._frame_counter

    def get_status(self):
        with self._lock:
            return self._status, self._error_message

    # ----- BackgroundWorker hooks --------------------------------------

    def tick(self):
        """Ensure we're connected, then read exactly one frame.

        The old _run/_read_loop pair was a loop inside a loop. This is
        the same state machine expressed one step at a time, so the base
        class can own the thread lifecycle.
        """
        if self._cap is None:
            if not self._open_capture():
                # _open_capture set status/error. Back off before the
                # retry; returns early if stop() lands during the wait.
                self._wait_or_stop(self.RETRY_DELAY_SECONDS)
                return
            self._set_status(StreamStatus.CONNECTED, None)
            self._consecutive_failures = 0
            self._consecutive_corrupt = 0
            self._last_good_small = None

        self._read_one_frame()

    def on_thread_exit(self):
        """Release the capture on the thread that opened it."""
        self._release_capture()

    def on_stop(self):
        """Runs on the caller's thread after the join. Only touches a
        lock-protected flag, which is safe there."""
        self._set_status(StreamStatus.STOPPED, None)

    # ----- internals --------------------------------------------------

    def _set_status(self, status, message):
        with self._lock:
            self._status = status
            self._error_message = message

    def _release_capture(self):
        cap, self._cap = self._cap, None
        if cap is not None:
            try:
                cap.release()
            except cv2.error as exc:
                print(f"[{self.LOG_TAG}] cam={self.cam_id} release failed: {exc}")

    def _drop_connection(self, message):
        """Tear down the capture and mark the stream for reconnection.
        Runs on the worker thread, so releasing here is safe."""
        self._release_capture()
        self._set_status(StreamStatus.ERROR, message)

    #: Params passed to every open attempt. Without these, FFmpeg falls
    #: back to its own ~30s default, and the worker sits uninterruptibly
    #: inside cap.open() for that whole time -- long enough that stop()
    #: times out its join and app shutdown visibly hangs.
    @classmethod
    def _open_params(cls):
        return [
            cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, cls.OPEN_TIMEOUT_MSEC,
            cv2.CAP_PROP_READ_TIMEOUT_MSEC, cls.READ_TIMEOUT_MSEC,
        ]

    def _open_capture(self):
        self._set_status(StreamStatus.CONNECTING, None)

        cap = cv2.VideoCapture()
        opened = False

        # Attempt 1: force the FFmpeg backend (works well for RTSP).
        try:
            opened = cap.open(self.url, cv2.CAP_FFMPEG, self._open_params())
        except cv2.error:
            opened = False

        # Attempt 2: let OpenCV choose the backend, which many HTTP /
        # MJPEG streams and local files need.
        #
        # The timeout params are passed here too. They used to be
        # omitted, so a camera that was merely unreachable -- rather
        # than actively refusing -- blocked this call for FFmpeg's full
        # ~30s default. On a network that blackholes packets instead of
        # sending a TCP reset (most real networks, and any firewalled
        # host), that made a dead camera freeze its worker for ~35s per
        # retry cycle, and stop() could not interrupt it.
        if not opened or not cap.isOpened():
            try:
                cap.release()
            except cv2.error:
                pass

            # Bail out between attempts if stop() has been requested.
            # Neither attempt can be interrupted once it is inside the C
            # call, so this checkpoint is the only place we can cut the
            # worst case in half: without it, stopping the app with an
            # unreachable camera waits out BOTH timeouts (2 x
            # OPEN_TIMEOUT_MSEC) before the thread can exit.
            if self._stop_event.is_set():
                self._set_status(StreamStatus.STOPPED, None)
                return False

            cap = cv2.VideoCapture()
            try:
                opened = cap.open(self.url, cv2.CAP_ANY, self._open_params())
            except cv2.error as exc:
                self._set_status(StreamStatus.ERROR, f"Failed to open stream: {exc}")
                return False

        if not cap.isOpened():
            try:
                cap.release()
            except cv2.error:
                pass
            self._set_status(
                StreamStatus.ERROR,
                "Could not open stream (bad URL or unreachable).",
            )
            return False

        self._cap = cap
        return True

    def _read_one_frame(self):
        cap = self._cap
        if cap is None:
            return

        try:
            ok, frame = cap.read()
        except cv2.error:
            # Decoder error on stream timeout or drop. Expected against
            # flaky cameras, so counted rather than logged per frame.
            self._count_failure()
            return

        if not ok or frame is None:
            self._count_failure()
            return

        small = cv2.resize(
            frame, self.CORRUPTION_THUMB_SIZE, interpolation=cv2.INTER_NEAREST
        )
        if self._is_corrupt(small):
            self._consecutive_corrupt += 1
            if self._consecutive_corrupt >= self.MAX_CONSECUTIVE_CORRUPT:
                self._drop_connection("Decoder not recovering, reconnecting...")
            return

        self._consecutive_failures = 0
        self._consecutive_corrupt = 0
        self._last_good_small = small
        with self._lock:
            self._latest_frame = frame
            self._frame_counter += 1

    def _count_failure(self):
        self._consecutive_failures += 1
        if self._consecutive_failures >= self.MAX_CONSECUTIVE_FAILURES:
            self._drop_connection("Stream disconnected, retrying...")

    def _is_corrupt(self, small):
        """Compare against the cached thumbnail of the last accepted
        frame. Comparing 64px thumbnails instead of full frames keeps the
        read loop fast enough to drain the stream in real time; caching
        the previous thumbnail halves the resize work."""
        if self._last_good_small is None:
            return False
        if small.shape != self._last_good_small.shape:
            return True
        diff = cv2.absdiff(small, self._last_good_small)
        return float(diff.mean()) > self.CORRUPTION_DIFF_THRESHOLD

    @classmethod
    def _looks_corrupt(cls, frame, last_good_frame):
        """Stateless corruption check on two full frames. Kept for
        callers and tests that compare frames directly."""
        if last_good_frame is None:
            return False
        if frame.shape != last_good_frame.shape:
            return True
        small_a = cv2.resize(
            frame, cls.CORRUPTION_THUMB_SIZE, interpolation=cv2.INTER_NEAREST
        )
        small_b = cv2.resize(
            last_good_frame, cls.CORRUPTION_THUMB_SIZE, interpolation=cv2.INTER_NEAREST
        )
        diff = cv2.absdiff(small_a, small_b)
        return float(diff.mean()) > cls.CORRUPTION_DIFF_THRESHOLD


class StreamManager(WorkerManager):
    """Owns one StreamWorker per active camera and exposes frame
    accessors keyed by camera id."""

    def _make_worker(self, cam_id, url=None):
        return StreamWorker(cam_id, url)

    # ----- back-compat aliases ------------------------------------------
    # Existing call sites in ui/app.py use these names. New code should
    # call start()/stop() from WorkerManager directly.

    def start_stream(self, cam_id, url):
        return self.start(cam_id, url=url)

    def stop_stream(self, cam_id):
        return self.stop(cam_id)

    # ----- frame accessors ------------------------------------------------

    def get_frame(self, cam_id):
        worker = self.get(cam_id)
        return None if worker is None else worker.get_frame()

    def get_frame_counter(self, cam_id):
        worker = self.get(cam_id)
        return 0 if worker is None else worker.get_frame_counter()

    def get_status(self, cam_id):
        worker = self.get(cam_id)
        if worker is None:
            return StreamStatus.STOPPED, None
        return worker.get_status()
