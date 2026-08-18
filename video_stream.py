"""
video_stream.py

Wraps cv2.VideoCapture in a background thread so reading frames from a
network stream (RTSP/TCP/UDP/HTTP) never blocks the Tkinter main loop.

Each StreamWorker continuously grabs the latest frame and stores it.
The GUI polls `get_frame()` on its own schedule (e.g. every 30ms) instead
of being driven by the network -- this means a slow/dead camera just
results in a stale or blank frame, not a frozen app.
"""

import threading
import time

import cv2


class StreamStatus:
    CONNECTING = "connecting"
    CONNECTED = "connected"
    ERROR = "error"
    STOPPED = "stopped"


class StreamWorker:
    """Background reader for a single camera URL."""

    # How long to wait before considering a connection attempt failed.
    # Without this, a dead RTSP/TCP target makes OpenCV/FFmpeg block for
    # ~30s by default before giving up.
    OPEN_TIMEOUT_MSEC = 5000
    READ_TIMEOUT_MSEC = 5000
    # Delay before retrying after a failed/dropped connection.
    RETRY_DELAY_SECONDS = 3

    def __init__(self, cam_id, url):
        self.cam_id = cam_id
        self.url = url

        self._cap = None
        self._thread = None
        self._lock = threading.Lock()

        self._latest_frame = None  # most recent BGR frame (numpy array) or None
        # Incremented every time a new decoded frame is stored.
        # Starts at 0 ("no frame yet") so any real frame (counter >= 1) is
        # distinguishable from "nothing decoded yet".
        self._frame_counter = 0
        self._status = StreamStatus.CONNECTING
        self._error_message = None

        self._stop_event = threading.Event()

    # ----- public API -----------------------------------------------

    def start(self):
        if self._thread is not None:
            return  # already running
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
        if self._cap is not None:
            try:
                self._cap.release()
            except Exception:
                pass
            self._cap = None
        with self._lock:
            self._status = StreamStatus.STOPPED

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

    # ----- internals --------------------------------------------------

    def _run(self):
        while not self._stop_event.is_set():
            opened = self._open_capture()
            if not opened:
                # _open_capture already set status/error; wait then retry
                if self._wait_or_stop(self.RETRY_DELAY_SECONDS):
                    return
                continue

            with self._lock:
                self._status = StreamStatus.CONNECTED
                self._error_message = None

            self._read_loop()

            # _read_loop exits when the stream drops or stop is requested
            if self._cap is not None:
                try:
                    self._cap.release()
                except Exception:
                    pass
                self._cap = None

            if self._stop_event.is_set():
                return

            with self._lock:
                self._status = StreamStatus.ERROR
                self._error_message = "Stream disconnected, retrying..."

            if self._wait_or_stop(self.RETRY_DELAY_SECONDS):
                return

    def _open_capture(self):
        with self._lock:
            self._status = StreamStatus.CONNECTING
            self._error_message = None

        cap = cv2.VideoCapture()
        opened = False

        # Attempt 1: Open with explicit FFmpeg timeout parameters (works well for RTSP streams)
        try:
            opened = cap.open(
                self.url,
                cv2.CAP_FFMPEG,
                [
                    cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, self.OPEN_TIMEOUT_MSEC,
                    cv2.CAP_PROP_READ_TIMEOUT_MSEC, self.READ_TIMEOUT_MSEC,
                ],
            )
        except Exception:
            opened = False

        # Attempt 2: Fall back to standard open if parameterized open failed (necessary for many HTTP / MJPEG streams)
        if not opened or not cap.isOpened():
            try:
                cap.release()
            except Exception:
                pass
            cap = cv2.VideoCapture()
            try:
                opened = cap.open(self.url)
            except Exception as exc:
                with self._lock:
                    self._status = StreamStatus.ERROR
                    self._error_message = f"Failed to open stream: {exc}"
                return False

        if not cap.isOpened():
            try:
                cap.release()
            except Exception:
                pass
            with self._lock:
                self._status = StreamStatus.ERROR
                self._error_message = "Could not open stream (bad URL or unreachable)."
            return False

        self._cap = cap
        return True

    def _read_loop(self):
        consecutive_failures = 0
        max_consecutive_failures = 20  # treat as dropped after this many bad reads
        consecutive_corrupt = 0
        max_consecutive_corrupt = 10  # don't get stuck rejecting forever
        last_good_frame = None

        while not self._stop_event.is_set():
            if self._cap is None:
                return

            try:
                ok, frame = self._cap.read()
            except (cv2.error, Exception):
                # Handle C++ exception / OpenCV error on stream timeout or drop
                consecutive_failures += 1
                if consecutive_failures >= max_consecutive_failures:
                    return  # signal caller to reconnect
                time.sleep(0.05)
                continue

            if not ok or frame is None:
                consecutive_failures += 1
                if consecutive_failures >= max_consecutive_failures:
                    return  # signal caller to reconnect
                time.sleep(0.05)
                continue

            if self._looks_corrupt(frame, last_good_frame):
                consecutive_corrupt += 1
                if consecutive_corrupt >= max_consecutive_corrupt:
                    return  # decoder isn't recovering, force a reconnect
                time.sleep(0.05)
                continue

            consecutive_failures = 0
            consecutive_corrupt = 0
            last_good_frame = frame
            with self._lock:
                self._latest_frame = frame
                self._frame_counter += 1

    CORRUPTION_DIFF_THRESHOLD = 70.0

    @classmethod
    def _looks_corrupt(cls, frame, last_good_frame):
        if last_good_frame is None:
            return False
        if frame.shape != last_good_frame.shape:
            return True
        # Compare a small downsampled thumbnail instead of the full frame --
        # a corrupt/garbled frame is just as detectable at 64px as at full
        # resolution, and this keeps the read loop fast enough to actually
        # drain the stream in real time instead of falling behind it.
        small_a = cv2.resize(frame, (64, 64), interpolation=cv2.INTER_NEAREST)
        small_b = cv2.resize(last_good_frame, (64, 64), interpolation=cv2.INTER_NEAREST)
        diff = cv2.absdiff(small_a, small_b)
        return float(diff.mean()) > cls.CORRUPTION_DIFF_THRESHOLD

    def _wait_or_stop(self, seconds):
        """Sleep up to `seconds`, but return True early if stop() was called."""
        return self._stop_event.wait(timeout=seconds)


class StreamManager:
    """Owns one StreamWorker per active camera and exposes simple
    start/stop/get_frame helpers keyed by camera id."""

    def __init__(self):
        self._workers = {}  # cam_id -> StreamWorker

    def start_stream(self, cam_id, url):
        self.stop_stream(cam_id)  # ensure no duplicate worker for this id
        worker = StreamWorker(cam_id, url)
        worker.start()
        self._workers[cam_id] = worker

    def stop_stream(self, cam_id):
        worker = self._workers.pop(cam_id, None)
        if worker is not None:
            worker.stop()

    def stop_all(self):
        for cam_id in list(self._workers.keys()):
            self.stop_stream(cam_id)

    def get_frame(self, cam_id):
        worker = self._workers.get(cam_id)
        if worker is None:
            return None
        return worker.get_frame()

    def get_frame_counter(self, cam_id):
        worker = self._workers.get(cam_id)
        if worker is None:
            return 0
        return worker.get_frame_counter()

    def get_status(self, cam_id):
        worker = self._workers.get(cam_id)
        if worker is None:
            return StreamStatus.STOPPED, None
        return worker.get_status()

    def active_camera_ids(self):
        return list(self._workers.keys())