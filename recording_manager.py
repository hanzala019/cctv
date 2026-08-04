"""
recording_manager.py

Phase 7: continuous local recording. One RecordingWorker per camera,
same start/stop background-thread lifecycle every other manager in
this app has (StreamManager, MotionManager, ObjectDetectionManager,
AlertManager) -- pulls the latest decoded frame from StreamManager on
its own cadence, writes it via cv2.VideoWriter, and rolls to a new
file every SEGMENT_DURATION_SECONDS. Always-on for every camera (see
Phase 7 design discussion) -- no per-camera enable switch, no Settings
UI, camera_store.py is untouched by this phase.

Segment rolling & the SQLite index
-----------------------------------
Every time a segment opens, a row goes into event_store's `segments`
table immediately (end_time still NULL -- see EventStore.start_segment)
so a segment that's still being written is already discoverable, not
just ones that have finished. When it rolls (or the worker stops),
that row is closed out with its end_time and final file size.

get_current_segment_id(cam_id) is the one piece of public API other
Phase 7 pieces (EventLoggerWorker, and MainWindow's object-detection
event logging) actually need -- it's how a detection at a given moment
gets tagged with the segment it'll end up living in, without those
callers needing to know anything about file paths or the writer
itself.

File layout
-----------
    <FOOTAGE_ROOT>/<camera_id>/<start_time>.mp4

matching the roadmap's example layout, using each camera's id (stable,
filesystem-safe, already used elsewhere as a directory-safe key)
rather than its display name (which can contain spaces/slashes and can
be renamed, which would orphan old segments' implied camera folder).

Known limitation: no runtime test environment in the build sandbox --
verified via manual tracing against the cv2.VideoWriter API shape, not
against a live camera, same caveat object_detector.py already carries
for ultralytics.
"""

import os
import threading
import time
from datetime import datetime

import cv2

from event_store import EventStore

# Beside the project's .py files, not the terminal's current working
# directory -- os.getcwd() would put footage in a different place every
# time depending on where you happened to launch `python main.py` from
# (project root vs. a parent folder vs. wherever your IDE's run
# config defaults to), which is worse than the original home-directory
# bug, not better. Anchoring to this file's own location means the
# footage folder always lands next to camera_store.py/main.py/etc.
# regardless of launch directory.
_PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
FOOTAGE_ROOT = os.path.join(_PROJECT_DIR, "cctv_viewer_footage")

SEGMENT_DURATION_SECONDS = 30 * 60  # 30 minutes, fixed -- see Phase 7 design discussion

# Frame pull/write cadence. StreamWorker doesn't do frame-rate pacing
# (it just always holds "the latest frame"), so recording at a fixed
# cadence rather than trying to match each camera's real decode rate
# means a slow camera's segment will contain some duplicate frames --
# an acceptable tradeoff for a first pass (the roadmap's "Suggested
# approach" isn't aiming for broadcast-accurate timing, just a working
# local archive).
RECORD_FPS = 15
RECORD_INTERVAL_SECONDS = 1.0 / RECORD_FPS

RETENTION_DAYS = 14  # fixed, no per-camera UI -- see Phase 7 design discussion

# Segment thumbnail: a small JPEG captured from the first frame of each
# segment, saved as a sibling file (same name, "_thumb.jpg" instead of
# the video extension) -- deterministic from file_path, so no DB
# column is needed; RecordingsSectionPanel derives the path itself via
# thumbnail_path_for() the same way it already derives "does the video
# file exist" from file_path.
THUMBNAIL_MAX_DIM = 240
THUMBNAIL_JPEG_QUALITY = 80


def thumbnail_path_for(file_path):
    return os.path.splitext(file_path)[0] + "_thumb.jpg"


def _save_segment_thumbnail(frame_bgr, file_path):
    """Best-effort -- a failed thumbnail write shouldn't take down
    recording itself, so this only logs, never raises."""
    try:
        h, w = frame_bgr.shape[:2]
        if max(h, w) > THUMBNAIL_MAX_DIM:
            scale = THUMBNAIL_MAX_DIM / float(max(h, w))
            frame_bgr = cv2.resize(frame_bgr, (max(1, int(w * scale)), max(1, int(h * scale))))
        cv2.imwrite(
            thumbnail_path_for(file_path), frame_bgr,
            [int(cv2.IMWRITE_JPEG_QUALITY), THUMBNAIL_JPEG_QUALITY],
        )
    except Exception as exc:  # pragma: no cover - defensive
        print(f"[recording_manager] couldn't save thumbnail for {file_path}: {exc}")

# Candidate (fourcc, extension) pairs tried in order when opening a new
# segment's VideoWriter -- not every OpenCV build has every codec
# available. opencv-python wheels commonly ship FFmpeg without libx264
# (licensing), so "avc1"/H.264 -- the codec most video players actually
# support well -- can silently fail to open even though the fourcc
# string itself is valid. "mp4v" (MPEG-4 Part 2) almost always opens,
# but plenty of default OS players (Windows Media Player especially)
# report exactly "file may be corrupted or use an unsupported format"
# for it. XVID in an .avi container is the most broadly compatible
# fallback across OpenCV builds when neither MP4 option works.
#
# Bug fix: this used to just assume cv2.VideoWriter(..., "mp4v", ...)
# succeeded and write to it regardless -- cv2.VideoWriter doesn't raise
# on an unsupported codec, it just silently produces a non-functional
# writer, so every .write() call was a no-op and segments could end up
# empty or unplayable with zero indication anywhere. Now each candidate
# is checked with .isOpened() before being trusted, and camera_dir's
# 0-byte leftover from a failed attempt is cleaned up before trying the
# next one.
CODEC_CANDIDATES = [
    ("avc1", ".mp4"),
    ("mp4v", ".mp4"),
    ("XVID", ".avi"),
]

RETENTION_CHECK_INTERVAL_SECONDS = 60 * 60  # sweep for expired footage once an hour

OPEN_RETRY_BACKOFF_SECONDS = 30  # how long to wait before retrying after every codec candidate failed

# Forced early rollover: when a fresh object detection fires, the
# segment recording it won't be playable (moov atom unfinalized) until
# it closes -- which could be up to SEGMENT_DURATION_SECONDS away. To
# make a just-detected moment reviewable soon instead of possibly 30
# minutes later, request_early_rollover() schedules the CURRENT
# segment to close a short buffer from now (giving the clip a few
# seconds of "after" context instead of cutting off exactly at the
# trigger), rather than waiting for a full segment's worth. Rate-
# limited so a burst of several detections in a row doesn't fragment
# recording into a pile of near-empty files -- only the first request
# in any given window actually schedules a rollover.
FORCED_ROLLOVER_DELAY_SECONDS = 15
MIN_FORCED_ROLLOVER_GAP_SECONDS = 120


class RecordingStatus:
    NO_FRAME = "no_frame"        # camera has no decoded frame yet -- mirrors MotionStatus.NO_FRAME
    RECORDING = "recording"      # actively writing to an open segment
    CODEC_ERROR = "codec_error"  # every codec candidate failed to open -- recording is not working
    STOPPED = "stopped"

# Once a codec is confirmed to open successfully, remembered here so
# every later segment (across every camera) tries it first instead of
# re-running the whole fallback chain every 30 minutes -- codec support
# is a property of the installed OpenCV/FFmpeg build, not something
# that changes camera to camera or segment to segment.
_working_codec = None


def _iso_now():
    return datetime.now().isoformat(timespec="seconds")


def _safe_filename_timestamp():
    # Colons aren't valid in Windows filenames -- avoid them entirely
    # rather than special-casing by platform.
    return datetime.now().strftime("%Y-%m-%d_%H-%M-%S")


class RecordingWorker:
    """Background recorder for a single camera."""

    def __init__(self, cam_id, stream_manager, event_store):
        self.cam_id = cam_id
        self.stream_manager = stream_manager
        self.event_store = event_store

        self._thread = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()

        self._writer = None
        self._writer_frame_size = None  # (w, h) the current writer was opened with
        self._current_segment_id = None
        self._segment_started_at = None
        self._current_file_path = None
        # If every codec candidate fails, don't retry on literally
        # every frame (~15x/sec) -- back off and only retry (and only
        # log) once per this interval until it starts working.
        self._next_open_attempt_at = 0.0
        self._status = RecordingStatus.NO_FRAME

        # Forced early rollover state -- see request_early_rollover().
        self._rollover_requested_at = None  # timestamp a rollover was scheduled for, or None
        self._last_forced_rollover_at = 0.0

    # ----- public API ----------------------------------------------

    def start(self):
        if self._thread is not None:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        print(f"[recording_manager] started recording worker for cam={self.cam_id}")

    def stop(self):
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        self._close_current_segment()
        with self._lock:
            self._status = RecordingStatus.STOPPED

    def get_current_segment_id(self):
        """The segment_id an event happening right now should be
        tagged with, or None if this camera hasn't opened its first
        segment yet (e.g. still starting up)."""
        with self._lock:
            return self._current_segment_id

    def get_status(self):
        """Current RecordingStatus -- polled by CameraTile the same
        cadence StreamStatus/MotionStatus already are, to drive the
        caption-bar recording-error indicator. See RecordingStatus's
        docstring-level comments for what each state means."""
        with self._lock:
            return self._status

    def request_early_rollover(self):
        """Called when something worth reviewing soon just happened --
        today, only a fresh object detection (see main.py's
        _log_detection_events). Schedules the current segment to close
        FORCED_ROLLOVER_DELAY_SECONDS from now instead of waiting out
        the full SEGMENT_DURATION_SECONDS, so the clip containing that
        moment becomes playable in under a minute instead of possibly
        up to 30 minutes -- see the module-level comment above
        FORCED_ROLLOVER_DELAY_SECONDS for the full reasoning. Rate-
        limited to at most once per MIN_FORCED_ROLLOVER_GAP_SECONDS,
        and a no-op if a rollover is already scheduled."""
        with self._lock:
            now = time.time()
            if now - self._last_forced_rollover_at < MIN_FORCED_ROLLOVER_GAP_SECONDS:
                return
            if self._rollover_requested_at is not None:
                return  # already scheduled -- don't push it back out
            self._rollover_requested_at = now + FORCED_ROLLOVER_DELAY_SECONDS

    # ----- internals -------------------------------------------------

    def _run(self):
        while not self._stop_event.is_set():
            try:
                frame = self.stream_manager.get_frame(self.cam_id)
                if frame is None:
                    with self._lock:
                        self._status = RecordingStatus.NO_FRAME
                else:
                    self._write_frame(frame)
                    with self._lock:
                        self._status = (
                            RecordingStatus.RECORDING if self._writer is not None
                            else RecordingStatus.CODEC_ERROR
                        )
            except Exception as exc:  # pragma: no cover - defensive
                # Bug fix: this loop previously had no try/except, so
                # any failure here (bad codec, permission error, disk
                # full) silently killed the thread -- Python prints an
                # unhandled-thread-exception traceback to stderr by
                # default, but that's easy to miss if you're only
                # watching stdout, and the thread would then just never
                # run again with zero indication why. Now it logs and
                # keeps trying on the next tick instead of dying.
                print(f"[recording_manager] cam={self.cam_id} write error: {exc}")
            if self._wait_or_stop(RECORD_INTERVAL_SECONDS):
                return

    def _write_frame(self, frame_bgr):
        now = time.time()

        if self._writer is None:
            if now < self._next_open_attempt_at:
                return  # backing off after a recent total codec failure
            self._open_new_segment(frame_bgr)
        elif now - self._segment_started_at >= SEGMENT_DURATION_SECONDS:
            self._close_current_segment()
            self._open_new_segment(frame_bgr)
        elif self._rollover_requested_at is not None and now >= self._rollover_requested_at:
            with self._lock:
                self._last_forced_rollover_at = now
                self._rollover_requested_at = None
            self._close_current_segment()
            self._open_new_segment(frame_bgr)
        elif frame_bgr.shape[1::-1] != self._writer_frame_size:
            # Camera resolution changed mid-segment (reconnect at a
            # different resolution) -- cv2.VideoWriter can't handle a
            # frame size change in-place, so roll to a fresh segment
            # rather than silently dropping/corrupting frames.
            self._close_current_segment()
            self._open_new_segment(frame_bgr)

        if self._writer is not None and (frame_bgr.shape[1], frame_bgr.shape[0]) == self._writer_frame_size:
            self._writer.write(frame_bgr)

    def _open_new_segment(self, first_frame_bgr):
        cam_dir = os.path.join(FOOTAGE_ROOT, self.cam_id)
        os.makedirs(cam_dir, exist_ok=True)

        h, w = first_frame_bgr.shape[:2]
        writer, file_path = self._open_writer(cam_dir, w, h)
        if writer is None:
            # Every candidate codec failed to open -- recording is
            # effectively broken on this machine's OpenCV/FFmpeg build.
            # Logged loudly (unlike the old silent-failure behavior)
            # rather than leaving a dangling writer that would have
            # accepted .write() calls into nothing, and backed off so
            # this doesn't retry/log on every single frame.
            print(
                f"[recording_manager] cam={self.cam_id} could not open any video "
                f"codec ({[c[0] for c in CODEC_CANDIDATES]}) -- recording is not "
                f"working for this camera. Check your OpenCV/FFmpeg build. "
                f"Retrying in {OPEN_RETRY_BACKOFF_SECONDS}s."
            )
            self._next_open_attempt_at = time.time() + OPEN_RETRY_BACKOFF_SECONDS
            return

        start_iso = _iso_now()
        segment_id = self.event_store.start_segment(self.cam_id, start_iso, file_path)
        _save_segment_thumbnail(first_frame_bgr, file_path)

        with self._lock:
            self._writer = writer
            self._writer_frame_size = (w, h)
            self._current_segment_id = segment_id
            self._segment_started_at = time.time()
            self._current_file_path = file_path

        writer.write(first_frame_bgr)

    def _open_writer(self, cam_dir, w, h):
        """Tries each codec candidate in order (the last one confirmed
        working, first, if there is one -- see _working_codec), and
        returns (writer, file_path) for the first one whose
        VideoWriter actually reports isOpened(). Returns (None, None)
        if every candidate fails."""
        global _working_codec

        candidates = list(CODEC_CANDIDATES)
        if _working_codec is not None and _working_codec in candidates:
            candidates.remove(_working_codec)
            candidates.insert(0, _working_codec)

        for fourcc_str, ext in candidates:
            filename = f"{_safe_filename_timestamp()}{ext}"
            file_path = os.path.join(cam_dir, filename)
            fourcc = cv2.VideoWriter_fourcc(*fourcc_str)
            writer = cv2.VideoWriter(file_path, fourcc, RECORD_FPS, (w, h))

            if writer.isOpened():
                if _working_codec != (fourcc_str, ext):
                    print(f"[recording_manager] using codec '{fourcc_str}' ({ext})")
                    _working_codec = (fourcc_str, ext)
                return writer, file_path

            writer.release()
            # cv2 sometimes creates a 0-byte file even when the writer
            # fails to open -- clean it up so a failed attempt doesn't
            # leave junk files scattered through the footage folder.
            if os.path.exists(file_path):
                try:
                    os.remove(file_path)
                except OSError:
                    pass

        return None, None

    def _close_current_segment(self):
        with self._lock:
            writer = self._writer
            segment_id = self._current_segment_id
            file_path = self._current_file_path
            self._writer = None
            self._writer_frame_size = None
            self._current_segment_id = None
            self._segment_started_at = None
            self._current_file_path = None

        if writer is None:
            return

        writer.release()

        file_size = None
        if file_path and os.path.exists(file_path):
            try:
                file_size = os.path.getsize(file_path)
            except OSError:
                file_size = None

        if segment_id is not None:
            self.event_store.close_segment(segment_id, _iso_now(), file_size)

    def _wait_or_stop(self, seconds):
        return self._stop_event.wait(timeout=seconds)


class _RetentionSweeper:
    """Owns the single background thread (not per-camera -- there's
    nothing camera-specific about "delete old files") that periodically
    deletes footage + DB rows older than RETENTION_DAYS. Split out of
    RecordingManager itself only to keep RecordingManager's per-camera
    start/stop bookkeeping uncluttered by this unrelated timer."""

    def __init__(self, event_store):
        self.event_store = event_store
        self._thread = None
        self._stop_event = threading.Event()

    def start(self):
        if self._thread is not None:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def _run(self):
        # Run one sweep shortly after startup too, not just after the
        # first full interval -- otherwise a footage directory that's
        # already over retention (app was off for a while) sits there
        # for up to an hour before the first cleanup.
        self._sweep()
        while not self._stop_event.wait(timeout=RETENTION_CHECK_INTERVAL_SECONDS):
            self._sweep()

    def _sweep(self):
        cutoff = datetime.now().timestamp() - RETENTION_DAYS * 86400
        cutoff_iso = datetime.fromtimestamp(cutoff).isoformat(timespec="seconds")
        try:
            deleted_paths = self.event_store.delete_segments_older_than(cutoff_iso)
            for path in deleted_paths:
                for target in (path, thumbnail_path_for(path)):
                    try:
                        if os.path.exists(target):
                            os.remove(target)
                    except OSError as exc:
                        print(f"[recording_manager] couldn't delete {target}: {exc}")
            self.event_store.delete_orphan_events_older_than(cutoff_iso)
        except Exception as exc:  # pragma: no cover - defensive
            print(f"[recording_manager] retention sweep failed: {exc}")


class RecordingManager:
    """Owns one RecordingWorker per camera plus the single retention
    sweeper thread, mirrors every other *Manager's shape in this app."""

    def __init__(self, stream_manager, event_store=None):
        self.stream_manager = stream_manager
        self.event_store = event_store or EventStore()
        self._workers = {}  # cam_id -> RecordingWorker
        self._retention = _RetentionSweeper(self.event_store)
        self._retention.start()

    def start_recording(self, cam_id):
        self.stop_recording(cam_id)
        worker = RecordingWorker(cam_id, self.stream_manager, self.event_store)
        worker.start()
        self._workers[cam_id] = worker

    def stop_recording(self, cam_id):
        worker = self._workers.pop(cam_id, None)
        if worker is not None:
            worker.stop()

    def stop_all(self):
        for cam_id in list(self._workers.keys()):
            self.stop_recording(cam_id)
        self._retention.stop()

    def get_current_segment_id(self, cam_id):
        worker = self._workers.get(cam_id)
        if worker is None:
            return None
        return worker.get_current_segment_id()

    def get_status(self, cam_id):
        worker = self._workers.get(cam_id)
        if worker is None:
            return RecordingStatus.STOPPED
        return worker.get_status()

    def request_early_rollover(self, cam_id):
        worker = self._workers.get(cam_id)
        if worker is not None:
            worker.request_early_rollover()

    def active_camera_ids(self):
        return list(self._workers.keys())