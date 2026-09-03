"""
Tests for the worker layer: core.worker.worker, core.worker.manager, and
all six per-camera workers built on them.

One file rather than one per worker, because they all test the same
thing -- the shared thread lifecycle -- and the interesting assertions
are the ones that apply to every worker at once. Those live in the
"contract" sections and are parametrised across all six, so adding a
seventh worker means adding one line to WORKERS and inheriting the
whole suite.

Layout:
    1. Fakes and fixtures
    2. BackgroundWorker  -- the base, via toy subclasses
    3. WorkerManager     -- the base, via a toy subclass
    4. Worker contract   -- parametrised across all six real workers
    5. Manager contract  -- parametrised across all five real managers
    6. Per-worker behaviour that isn't shared

Collaborators are faked. The point is the lifecycle, not re-testing
motion detection or ONNX inference. StreamWorker is the exception: it
runs against a real decoder reading a real generated video, because a
mocked capture would only test the mock.
"""

import threading
import time

import numpy as np
import pytest

from core.alerts.alert_manager import AlertManager, AlertWorker
from core.capture.video_stream import StreamManager, StreamStatus, StreamWorker
from core.detection.motion_detector import (
    MotionManager,
    MotionResult,
    MotionStatus,
    MotionWorker,
)
from core.detection.object_detector import ObjectDetectionManager, ObjectDetectionWorker
from core.recording.event_logger import EventLoggerManager, EventLoggerWorker
from core.recording.recording_manager import RecordingWorker
from core.worker.manager import WorkerManager
from core.worker.worker import BackgroundWorker

# ===================================================================
# 1. Fakes and fixtures
# ===================================================================


def wait_until(predicate, timeout=15.0, interval=0.01):
    """Poll until predicate() returns something other than None/False.

    Deliberately not plain truthiness: a numpy frame raises ValueError
    in a boolean context, and an all-black frame would read as falsy.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = predicate()
        if result is not None and result is not False:
            return result
        time.sleep(interval)
    return predicate()


@pytest.fixture(autouse=True)
def _no_leaked_threads(request):
    """Fail the test that leaks a thread, rather than the innocent test
    that runs after it.

    A worker blocked inside a C call (cv2 open/read) keeps running after
    stop() gives up joining it, and can slow or break later tests. This
    pins the blame where it belongs. Only reports threads this test
    started, and gives them a moment to wind down first.
    """
    before = {t.ident for t in threading.enumerate()}
    yield
    if request.node.get_closest_marker("allow_thread_leak"):
        # This test deliberately exercises a thread that may still be
        # inside a blocking C call. It makes its own assertion about
        # when that thread exits.
        return
    deadline = time.time() + 2.0
    while time.time() < deadline:
        leaked = [t for t in threading.enumerate()
                  if t.ident not in before and t.is_alive()]
        if not leaked:
            return
        time.sleep(0.05)
    names = [t.name for t in leaked]
    if names:
        pytest.fail(f"test leaked still-running threads: {names}")


@pytest.fixture(scope="session")
def video_file(tmp_path_factory):
    """A real, decodable video file for StreamWorker to read.

    Generated rather than committed, so the suite has no binary
    fixtures and no dependency on a camera being reachable.
    """
    import cv2

    path = tmp_path_factory.mktemp("video") / "test.mp4"
    writer = cv2.VideoWriter(
        str(path), cv2.VideoWriter_fourcc(*"mp4v"), 30, (320, 240)
    )
    for i in range(120):
        frame = np.zeros((240, 320, 3), np.uint8)
        frame[:, :, 0] = (i * 2) % 255
        cv2.rectangle(frame, (i, 50), (i + 40, 90), (0, 255, 0), -1)
        writer.write(frame)
    writer.release()
    assert path.exists() and path.stat().st_size > 0
    return str(path)


class FakeStreamManager:
    def __init__(self, frame=None):
        self.frame = frame

    def get_frame(self, cam_id):
        return self.frame


class FakeMotionManager:
    def __init__(self, motion=False):
        self.result = MotionResult(motion=motion)

    def get_result(self, cam_id):
        return self.result

    def set_motion(self, value):
        self.result = MotionResult(motion=value)


class FakeCameraStore:
    def __init__(self, camera=None):
        self.camera = camera if camera is not None else {
            "id": "cam1",
            "motion_enabled": False,
            "object_detection_enabled": False,
            "zones": [],
            "alert_rules": [],
        }

    def get_camera(self, cam_id):
        return self.camera


class FakeRecordingManager:
    def get_current_segment_id(self, cam_id):
        return "seg1"

class FakeSettingsStore:
    def get_recording_path(self):
        return None
    def get_settings_info(self):
        return {"duration_minutes": 30.0, "retention_days": 14}

class FakeEventStore:
    """Records calls so tests can assert on what a worker wrote."""

    def __init__(self):
        self._lock = threading.Lock()
        self.events = []
        self.segments_opened = []
        self.segments_closed = []

    def add_event(self, **kwargs):
        with self._lock:
            self.events.append(kwargs)
        return f"evt{len(self.events)}"

    def start_segment(self, cam_id, start_iso, file_path):
        with self._lock:
            self.segments_opened.append((cam_id, file_path))
        return f"seg{len(self.segments_opened)}"

    def close_segment(self, segment_id, end_iso, file_size):
        with self._lock:
            self.segments_closed.append(segment_id)

    def delete_segments_older_than(self, cutoff_iso):
        return []

    def delete_orphan_events_older_than(self, cutoff_iso):
        return 0

    def event_classes(self):
        with self._lock:
            return [e["detection_class"] for e in self.events]


# --- worker/manager construction, so the contract tests can be generic

def build_worker(cls, video_file=None):
    """Construct any worker with fakes. Central so the parametrised
    contract tests below don't each need a factory."""
    if cls is StreamWorker:
        return cls("cam1", video_file)
    if cls is MotionWorker:
        return cls("cam1", FakeStreamManager(), FakeCameraStore())
    if cls is ObjectDetectionWorker:
        return cls("cam1", FakeStreamManager(), FakeMotionManager(), FakeCameraStore())
    if cls is AlertWorker:
        return cls("cam1", FakeMotionManager(), None, FakeCameraStore(), channels=[])
    if cls is EventLoggerWorker:
        return cls("cam1", FakeMotionManager(), FakeRecordingManager(), FakeEventStore())
    if cls is RecordingWorker:
        return cls("cam1", FakeStreamManager(), FakeEventStore())
    raise AssertionError(f"no builder for {cls}")


def build_manager(cls):
    if cls is StreamManager:
        return cls()
    if cls is MotionManager:
        return cls(FakeStreamManager(), FakeCameraStore())
    if cls is ObjectDetectionManager:
        return cls(FakeStreamManager(), FakeMotionManager(), FakeCameraStore())
    if cls is AlertManager:
        return cls(FakeMotionManager(), None, FakeCameraStore(), channels=[])
    if cls is EventLoggerManager:
        return cls(FakeMotionManager(), FakeRecordingManager(), FakeEventStore())
    raise AssertionError(f"no builder for {cls}")


#: Every per-camera worker. Add a new one here and it inherits the
#: whole contract suite below.
WORKERS = [
    StreamWorker,
    MotionWorker,
    ObjectDetectionWorker,
    AlertWorker,
    EventLoggerWorker,
    RecordingWorker,
]

#: RecordingManager is excluded: it starts a retention sweeper thread in
#: __init__ and touches the real EventStore, so it is covered by its own
#: tests rather than the generic contract.
MANAGERS = [
    StreamManager,
    MotionManager,
    ObjectDetectionManager,
    AlertManager,
    EventLoggerManager,
]

by_name = lambda cls: cls.__name__  # noqa: E731 -- pytest id function


@pytest.fixture
def worker(request, video_file):
    """Builds the parametrised worker and guarantees it is stopped,
    even if the test fails -- otherwise one failure leaks a thread and
    every later thread-count assertion fails too."""
    instance = build_worker(request.param, video_file)
    yield instance
    instance.stop()


@pytest.fixture
def manager(request):
    instance = build_manager(request.param)
    yield instance
    instance.stop_all()


# ===================================================================
# 2. BackgroundWorker
# ===================================================================


class CountingWorker(BackgroundWorker):
    INTERVAL_SECONDS = 0.01
    LOG_TAG = "test"

    def __init__(self, cam_id="cam1"):
        super().__init__(cam_id)
        self.ticks = 0
        self.thread_exit_ran_on = None
        self.stop_ran_on = None

    def tick(self):
        self.ticks += 1

    def on_thread_exit(self):
        self.thread_exit_ran_on = threading.current_thread().name

    def on_stop(self):
        self.stop_ran_on = threading.current_thread().name


class ExplodingWorker(CountingWorker):
    def tick(self):
        self.ticks += 1
        raise RuntimeError("boom")


def test_base_tick_is_called_repeatedly():
    w = CountingWorker()
    w.start()
    try:
        assert wait_until(lambda: w.ticks > 3)
    finally:
        w.stop()


def test_base_running_reflects_thread_state():
    w = CountingWorker()
    assert w.running is False
    w.start()
    try:
        assert w.running is True
    finally:
        w.stop()
    assert w.running is False


def test_base_stop_halts_ticking():
    w = CountingWorker()
    w.start()
    wait_until(lambda: w.ticks > 2)
    w.stop()
    settled = w.ticks
    time.sleep(0.2)
    assert w.ticks == settled


def test_base_exception_in_tick_does_not_kill_the_thread():
    """A transient failure must not silently end the worker. In the old
    hand-rolled loops that looked exactly like a camera going quiet."""
    w = ExplodingWorker()
    w.start()
    try:
        assert wait_until(lambda: w.ticks > 3)
        assert w.running is True
    finally:
        w.stop()


def test_base_exception_in_tick_is_logged(capfd):
    w = ExplodingWorker()
    w.start()
    wait_until(lambda: w.ticks > 1)
    w.stop()
    out = capfd.readouterr().out
    assert "tick error" in out and "boom" in out


def test_base_tick_must_be_implemented():
    class Incomplete(BackgroundWorker):
        INTERVAL_SECONDS = 0.01

    with pytest.raises(NotImplementedError):
        Incomplete("cam1").tick()


def test_base_on_thread_exit_runs_on_the_worker_thread():
    """The segfault fix. Releasing a VideoCapture or VideoWriter from
    the caller's thread while the worker is still inside a C call is a
    crash, not an exception -- so cleanup must happen here."""
    w = CountingWorker("cam1")
    w.start()
    wait_until(lambda: w.ticks > 1)
    w.stop()
    assert w.thread_exit_ran_on == "test-cam1"


def test_base_on_stop_runs_on_the_caller_thread():
    w = CountingWorker()
    w.start()
    wait_until(lambda: w.ticks > 1)
    w.stop()
    assert w.stop_ran_on == threading.current_thread().name


def test_base_on_thread_exit_runs_even_when_tick_raises():
    w = ExplodingWorker("cam1")
    w.start()
    wait_until(lambda: w.ticks > 1)
    w.stop()
    assert w.thread_exit_ran_on == "test-cam1"


def test_base_cleanup_error_is_logged_not_raised(capfd):
    class BadCleanup(CountingWorker):
        def on_thread_exit(self):
            raise RuntimeError("cleanup boom")

    w = BadCleanup()
    w.start()
    wait_until(lambda: w.ticks > 1)
    w.stop()  # must not raise
    assert "cleanup error" in capfd.readouterr().out


# ===================================================================
# 3. WorkerManager
# ===================================================================


class FakeManager(WorkerManager):
    def _make_worker(self, cam_id, **kwargs):
        return CountingWorker(cam_id)


def test_base_manager_start_registers_a_running_worker():
    m = FakeManager()
    w = m.start("cam1")
    try:
        assert w.running and m.get("cam1") is w
    finally:
        m.stop_all()


def test_base_manager_start_replaces_rather_than_duplicates():
    """The invariant that used to be a hand-written `self.stop(id)` at
    the top of every start method. Miss it once and every camera
    restart silently leaks a thread."""
    m = FakeManager()
    first = m.start("cam1")
    second = m.start("cam1")
    try:
        assert first is not second
        assert first.running is False
        assert len(m) == 1
    finally:
        m.stop_all()


def test_base_manager_stop_removes_and_stops():
    m = FakeManager()
    w = m.start("cam1")
    m.stop("cam1")
    assert w.running is False
    assert m.get("cam1") is None and "cam1" not in m


def test_base_manager_stop_all():
    m = FakeManager()
    workers = [m.start(f"cam{i}") for i in range(3)]
    m.stop_all()
    assert m.active_camera_ids() == [] and len(m) == 0
    assert all(not w.running for w in workers)


def test_base_manager_stop_unknown_is_safe():
    FakeManager().stop("nope")


def test_base_manager_make_worker_must_be_implemented():
    with pytest.raises(NotImplementedError):
        WorkerManager().start("cam1")


# ===================================================================
# 4. Worker contract -- every worker, same rules
# ===================================================================


@pytest.mark.parametrize("cls", WORKERS, ids=by_name)
def test_worker_subclasses_the_base(cls):
    assert issubclass(cls, BackgroundWorker)


@pytest.mark.parametrize("cls", WORKERS, ids=by_name)
def test_worker_implements_tick(cls):
    """A worker that forgot the _tick -> tick rename would inherit the
    base's NotImplementedError and fail once per interval, forever,
    while still looking alive."""
    assert cls.tick is not BackgroundWorker.tick


@pytest.mark.parametrize("cls", WORKERS, ids=by_name)
def test_worker_declares_its_own_log_tag(cls):
    """Otherwise every one of the six prints '[worker]' and log lines
    can't be traced to a subsystem."""
    assert cls.LOG_TAG != BackgroundWorker.LOG_TAG


@pytest.mark.parametrize("cls", WORKERS, ids=by_name)
def test_worker_interval_is_sane(cls):
    """Only StreamWorker may use 0, because its tick() blocks in
    cap.read() and the stream paces the loop. A polling worker at 0
    would spin a core at 100%."""
    if cls is StreamWorker:
        assert cls.INTERVAL_SECONDS == 0
    else:
        assert cls.INTERVAL_SECONDS > 0


@pytest.mark.parametrize("worker", WORKERS, ids=by_name, indirect=True)
def test_worker_starts_and_stops(worker):
    worker.start()
    assert worker.running is True
    worker.stop()
    assert worker.running is False


@pytest.mark.parametrize("worker", WORKERS, ids=by_name, indirect=True)
def test_worker_double_start_does_not_leak_a_thread(worker):
    before = threading.active_count()
    worker.start()
    worker.start()
    assert threading.active_count() - before == 1


@pytest.mark.parametrize("worker", WORKERS, ids=by_name, indirect=True)
def test_worker_thread_exits_on_stop(worker):
    before = threading.active_count()
    worker.start()
    time.sleep(0.05)
    worker.stop()
    time.sleep(0.3)
    assert threading.active_count() <= before


@pytest.mark.parametrize("worker", WORKERS, ids=by_name, indirect=True)
def test_worker_stop_is_idempotent(worker):
    worker.start()
    worker.stop()
    worker.stop()


@pytest.mark.parametrize("worker", WORKERS, ids=by_name, indirect=True)
def test_worker_stop_without_start_is_safe(worker):
    worker.stop()


@pytest.mark.parametrize("worker", WORKERS, ids=by_name, indirect=True)
def test_worker_stop_is_prompt(worker):
    """Shutdown calls stop() six times per camera. If any one waits out
    its full interval, closing the app visibly hangs."""
    worker.start()
    time.sleep(0.05)
    started = time.time()
    worker.stop()
    assert time.time() - started < 2.0


@pytest.mark.parametrize("worker", WORKERS, ids=by_name, indirect=True)
def test_worker_thread_is_named_for_its_subsystem(worker):
    """Named threads are the difference between a readable and an
    unreadable stack dump with 90+ threads running."""
    worker.start()
    names = [t.name for t in threading.enumerate()]
    assert f"{type(worker).LOG_TAG}-cam1" in names


# ===================================================================
# 5. Manager contract -- every manager, same rules
# ===================================================================


@pytest.mark.parametrize("cls", MANAGERS, ids=by_name)
def test_manager_subclasses_the_base(cls):
    assert issubclass(cls, WorkerManager)


@pytest.mark.parametrize("cls", MANAGERS, ids=by_name)
def test_manager_implements_make_worker(cls):
    assert cls._make_worker is not WorkerManager._make_worker


@pytest.mark.parametrize("manager", MANAGERS, ids=by_name, indirect=True)
def test_manager_registry_tracks_active_cameras(manager, video_file):
    manager.start("cam1", url=video_file) if isinstance(manager, StreamManager) \
        else manager.start("cam1")
    assert manager.is_active("cam1")
    assert manager.active_camera_ids() == ["cam1"]
    manager.stop("cam1")
    assert not manager.is_active("cam1")
    assert manager.active_camera_ids() == []


@pytest.mark.parametrize("manager", MANAGERS, ids=by_name, indirect=True)
def test_manager_stop_all_clears_registry(manager, video_file):
    for cam in ("cam1", "cam2"):
        manager.start(cam, url=video_file) if isinstance(manager, StreamManager) \
            else manager.start(cam)
    manager.stop_all()
    assert manager.active_camera_ids() == []
    assert len(manager) == 0


@pytest.mark.parametrize("manager", MANAGERS, ids=by_name, indirect=True)
def test_manager_stop_unknown_camera_is_safe(manager):
    manager.stop("nope")


# ===================================================================
# 6. Per-worker behaviour
# ===================================================================

# --- StreamWorker: real decoder, real file ---------------------------


def test_stream_worker_delivers_frames(video_file):
    w = StreamWorker("cam1", video_file)
    w.start()
    try:
        frame = wait_until(w.get_frame)
        assert frame is not None and frame.shape == (240, 320, 3)
    finally:
        w.stop()


def test_stream_frame_counter_advances(video_file):
    """The counter is how CameraTile decides whether to repaint. If it
    stops advancing, the UI silently freezes on a stale frame."""
    w = StreamWorker("cam1", video_file)
    w.start()
    try:
        wait_until(w.get_frame)
        first = w.get_frame_counter()
        assert first >= 1
        assert wait_until(lambda: w.get_frame_counter() > first) is True
    finally:
        w.stop()


def test_stream_status_reaches_connected_then_stopped(video_file):
    w = StreamWorker("cam1", video_file)
    w.start()
    wait_until(lambda: w.get_status()[0] == StreamStatus.CONNECTED)
    assert w.get_status()[0] == StreamStatus.CONNECTED
    w.stop()
    assert w.get_status()[0] == StreamStatus.STOPPED


def test_stream_capture_is_released_on_stop(video_file):
    """The segfault fix, at the StreamWorker level: the handle must be
    gone after stop(), released by on_thread_exit rather than by stop()
    reaching across threads into a live capture."""
    w = StreamWorker("cam1", video_file)
    w.start()
    try:
        assert wait_until(w.get_frame) is not None, (
            "no frame decoded within the wait window -- the capture never "
            "opened, so this test cannot say anything about release"
        )
        assert w._cap is not None
    finally:
        w.stop()
    assert w._cap is None


#: An address that fails fast on every platform. Nothing listens on
#: port 1 of loopback, so the OS returns a TCP reset immediately.
#:
#: Do NOT use a blackholed public address here (TEST-NET-1, 192.0.2.x).
#: Those drop packets rather than refusing, so the connect blocks until
#: a timeout expires -- on Windows that meant a 35s hang, a leaked
#: thread, and two unrelated tests failing as collateral.
UNREACHABLE_URL = "rtsp://127.0.0.1:1/nonexistent"


def test_stream_unreachable_camera_reports_error_and_keeps_retrying(monkeypatch):
    """A dead camera must not kill the worker -- it reports ERROR and
    keeps retrying, so the stream recovers by itself when the camera
    comes back.

    Driven by a fake capture rather than a real unreachable address.
    The timed version of this test failed in four different ways across
    Windows and Linux CI, because whether an unreachable host refuses
    instantly or blackholes until a timeout depends entirely on the
    network and the OS -- and on CI it left an ffmpeg thread wedged in
    a C call, which aborted the interpreter at shutdown (SIGABRT).

    The guarantees that test was reaching for are covered without any
    network or timing by this test plus the two below it.
    """
    import cv2 as _cv2

    class DeadCap:
        def open(self, url, *args):
            return False

        def isOpened(self):
            return False

        def release(self):
            pass

    monkeypatch.setattr(_cv2, "VideoCapture", lambda *a, **k: DeadCap())

    w = StreamWorker("cam1", "rtsp://unreachable.invalid/stream")
    w.start()
    try:
        assert wait_until(lambda: w.get_status()[0] == StreamStatus.ERROR)
        status, message = w.get_status()
        assert status == StreamStatus.ERROR
        assert message, "an error status must carry a message for the UI"
        assert w.get_frame() is None
        assert w.running is True, "worker must keep retrying, not die"
    finally:
        w.stop()


def test_open_capture_skips_the_second_attempt_when_stopping(monkeypatch):
    """Stopping mid-connect must not wait out a second open attempt.

    Neither attempt can be interrupted once inside the C call, so the
    checkpoint between them is the only place the worst case can be
    halved. Without it, quitting the app with an unreachable camera
    waits out 2 x OPEN_TIMEOUT_MSEC before the thread can exit.

    Counts attempts rather than timing them, so it behaves identically
    on every OS and network.
    """
    import cv2 as _cv2

    calls = []
    worker = StreamWorker("cam1", "rtsp://example.invalid/stream")

    class FakeCap:
        def open(self, url, *args):
            calls.append(url)
            worker._stop_event.set()  # stop() arrives while attempt 1 blocks
            return False

        def isOpened(self):
            return False

        def release(self):
            pass

    monkeypatch.setattr(_cv2, "VideoCapture", lambda *a, **k: FakeCap())

    assert worker._open_capture() is False
    assert len(calls) == 1, (
        f"expected the second attempt to be skipped once stop was "
        f"requested, but {len(calls)} attempts were made"
    )


def test_stream_both_open_attempts_get_timeout_params(monkeypatch):
    """Deterministic guard on the bug that made a dead camera hang for
    ~35s: the fallback open attempt used to omit the timeout params.

    Asserted by recording the calls rather than by timing a real
    connection, because whether an unreachable host refuses (fast) or
    blackholes (slow) depends on the network and the OS -- this failed
    on Windows while passing on Linux.
    """
    import cv2 as _cv2

    calls = []

    class FakeCap:
        def open(self, url, *args):
            calls.append(args)
            return False  # force the fallback, then overall failure

        def isOpened(self):
            return False

        def release(self):
            pass

    monkeypatch.setattr(_cv2, "VideoCapture", lambda *a, **k: FakeCap())

    worker = StreamWorker("cam1", "rtsp://example.invalid/stream")
    assert worker._open_capture() is False

    assert len(calls) == 2, f"expected two open attempts, got {len(calls)}"
    for i, args in enumerate(calls):
        assert len(args) == 2, f"attempt {i + 1} passed no params: {args}"
        params = args[1]
        assert _cv2.CAP_PROP_OPEN_TIMEOUT_MSEC in params, f"attempt {i + 1}"
        assert _cv2.CAP_PROP_READ_TIMEOUT_MSEC in params, f"attempt {i + 1}"



def test_stream_corruption_thumbnail_is_cached(video_file):
    """The read loop should resize one frame per iteration, not two."""
    w = StreamWorker("cam1", video_file)
    w.start()
    try:
        wait_until(w.get_frame)
        assert w._last_good_small is not None
        assert w._last_good_small.shape[:2] == StreamWorker.CORRUPTION_THUMB_SIZE
    finally:
        w.stop()


@pytest.mark.parametrize(
    "a_fill,b_fill,expected",
    [(0, None, False), (100, 105, False), (0, 255, True)],
    ids=["first-frame", "similar", "wildly-different"],
)
def test_stream_looks_corrupt(a_fill, b_fill, expected):
    a = np.full((240, 320, 3), a_fill, np.uint8)
    b = None if b_fill is None else np.full((240, 320, 3), b_fill, np.uint8)
    assert StreamWorker._looks_corrupt(a, b) is expected


def test_stream_looks_corrupt_rejects_shape_change():
    a = np.zeros((240, 320, 3), np.uint8)
    b = np.zeros((480, 640, 3), np.uint8)
    assert StreamWorker._looks_corrupt(a, b) is True


def test_stream_manager_serves_frames_and_defaults(video_file):
    m = StreamManager()
    m.start_stream("cam1", video_file)
    try:
        assert wait_until(lambda: m.get_frame("cam1")) is not None
        # unknown camera must return safe defaults, not raise
        assert m.get_frame("nope") is None
        assert m.get_frame_counter("nope") == 0
        assert m.get_status("nope")[0] == StreamStatus.STOPPED
    finally:
        m.stop_all()


# --- MotionWorker ----------------------------------------------------


def test_motion_worker_reports_no_frame():
    w = MotionWorker("cam1", FakeStreamManager(frame=None), FakeCameraStore())
    w.start()
    try:
        assert wait_until(lambda: w.get_status() == MotionStatus.NO_FRAME)
    finally:
        w.stop()


def test_motion_worker_stop_sets_stopped_status():
    w = MotionWorker("cam1", FakeStreamManager(), FakeCameraStore())
    w.start()
    w.stop()
    assert w.get_status() == MotionStatus.STOPPED


def test_motion_manager_unknown_camera_returns_defaults():
    m = MotionManager(FakeStreamManager(), FakeCameraStore())
    assert m.get_status("nope") == MotionStatus.STOPPED
    assert m.get_result("nope").motion is False
    m.notify_zones_changed("nope")  # must not raise


# --- EventLoggerWorker ------------------------------------------------


def test_event_logger_logs_motion_start():
    store = FakeEventStore()
    w = EventLoggerWorker("cam1", FakeMotionManager(motion=True),
                          FakeRecordingManager(), store)
    w.start()
    try:
        assert wait_until(lambda: "motion_start" in store.event_classes())
    finally:
        w.stop()


def test_event_logger_closes_dangling_presence_on_stop():
    """Behaviour that used to live in the hand-rolled stop(). Without
    it, shutting down mid-motion leaves a motion_start with no matching
    motion_end, forever."""
    store = FakeEventStore()
    w = EventLoggerWorker("cam1", FakeMotionManager(motion=True),
                          FakeRecordingManager(), store)
    w.start()
    wait_until(lambda: "motion_start" in store.event_classes())
    w.stop()
    assert store.event_classes().count("motion_end") == 1


def test_event_logger_writes_nothing_when_there_is_no_motion():
    store = FakeEventStore()
    w = EventLoggerWorker("cam1", FakeMotionManager(motion=False),
                          FakeRecordingManager(), store)
    w.start()
    time.sleep(0.2)
    w.stop()
    assert store.event_classes() == []


# --- ObjectDetectionManager -------------------------------------------


def test_object_detection_manager_accessors_default_safely():
    m = ObjectDetectionManager(FakeStreamManager(), FakeMotionManager(),
                               FakeCameraStore())
    m.start_detection("cam1")
    try:
        assert m.get_present_classes("cam1") == set()
        assert m.get_active_detections("cam1") == []
    finally:
        m.stop_all()
    assert m.get_present_classes("nope") == set()
    assert m.get_active_detections("nope") == []


# --- Back-compat aliases ----------------------------------------------


def test_legacy_alias_names_still_work(video_file):
    """ui/app.py calls these names. If an alias is dropped, the app
    breaks at camera-add time, not at import time -- so assert them."""
    checks = [
        (StreamManager(), "start_stream", "stop_stream", (video_file,)),
        (MotionManager(FakeStreamManager(), FakeCameraStore()),
         "start_detection", "stop_detection", ()),
        (ObjectDetectionManager(FakeStreamManager(), FakeMotionManager(),
                                FakeCameraStore()),
         "start_detection", "stop_detection", ()),
        (AlertManager(FakeMotionManager(), None, FakeCameraStore(), channels=[]),
         "start_alerts", "stop_alerts", ()),
        (EventLoggerManager(FakeMotionManager(), FakeRecordingManager(),
                            FakeEventStore()),
         "start_logging", "stop_logging", ()),
    ]
    for mgr, start_name, stop_name, extra in checks:
        getattr(mgr, start_name)("cam1", *extra)
        assert mgr.is_active("cam1"), f"{type(mgr).__name__}.{start_name} failed"
        getattr(mgr, stop_name)("cam1")
        assert not mgr.is_active("cam1"), f"{type(mgr).__name__}.{stop_name} failed"


# ===================================================================
# 7. Diagnostics
# ===================================================================


def test_cpu_is_attributed_per_camera_and_subsystem():
    """Per-worker CPU attribution depends entirely on BackgroundWorker
    naming threads '{LOG_TAG}-{cam_id}'. If that naming is ever dropped,
    every thread lands in '_other' and the breakdown is useless."""
    from core.diagnostics import ResourceMonitor

    monitor = ResourceMonitor()
    if not monitor.available:
        pytest.skip("psutil not installed")

    workers = [
        MotionWorker("frontdoor", FakeStreamManager(), FakeCameraStore()),
        AlertWorker("driveway", FakeMotionManager(), None, FakeCameraStore(),
                    channels=[]),
    ]
    for w in workers:
        w.start()
    try:
        monitor.sample_cpu()  # prime; first call has no delta
        time.sleep(0.4)
        by_cam = monitor.cpu_by_camera()

        assert "frontdoor" in by_cam
        assert "driveway" in by_cam
        assert "motion" in by_cam["frontdoor"]["workers"]
        assert "alert_manager" in by_cam["driveway"]["workers"]
    finally:
        for w in workers:
            w.stop()


def test_first_cpu_sample_returns_empty():
    """A single reading of cumulative CPU time is meaningless; the
    monitor must report nothing rather than a bogus number."""
    from core.diagnostics import ResourceMonitor

    monitor = ResourceMonitor()
    if not monitor.available:
        pytest.skip("psutil not installed")
    assert monitor.sample_cpu() == {}


def test_per_camera_memory_counts_frame_buffers(video_file):
    from core.diagnostics import per_camera_memory

    manager = StreamManager()
    manager.start_stream("cam1", video_file)
    try:
        wait_until(lambda: manager.get_frame("cam1"))
        mem = per_camera_memory({"stream": manager})
        assert mem["cam1"]["total_mb"] > 0
    finally:
        manager.stop_all()


def test_diagnostics_never_raise_on_empty_managers():
    """Diagnostics run on a timer in the UI. A crash here must never
    take the app down."""
    from core.diagnostics import ResourceMonitor, gpu_report, per_camera_memory

    assert per_camera_memory({}) == {}
    assert per_camera_memory({"stream": StreamManager()}) == {}
    assert isinstance(gpu_report(), str)
    assert isinstance(ResourceMonitor().report(managers={}), str)
