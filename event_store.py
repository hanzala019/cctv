"""
event_store.py

Phase 7: SQLite-backed index of recorded video segments and detection/
motion events. Mirrors camera_store.py's role as the single point of
contact for its persisted data -- every other module calls EventStore's
methods and never touches the sqlite3 connection directly, the same
"storage boundary" convention camera_store.py already established for
the JSON camera list.

Two tables (schema straight from the Phase 7 roadmap notes):

    segments -- one row per recorded video file. A row is inserted the
    moment a segment starts (end_time still NULL) and updated in place
    when the segment closes (rolls to a new file, camera stops, or app
    shuts down) with its end_time and final file size.

    events -- one row per loggable thing that happened on a camera:
    an object-detection hit (detection_class = the COCO class name,
    e.g. "person"), or a motion lifecycle edge (detection_class =
    "motion_start" / "motion_end" -- two rows per motion presence,
    mirroring alert_manager.py's START/END log-line pattern, since this
    table has no duration column of its own; duration is derivable
    later from the timestamp gap between a start/end pair with the
    same camera_id if that's ever needed).

Why sqlite3 directly instead of an ORM: this schema is small (two
tables, no migrations expected beyond what's in _SCHEMA below) and the
roadmap explicitly calls for stdlib sqlite3 -- no new dependency for
what's fundamentally a local index, not an app database.

Threading: every method opens its own short-lived connection rather
than sharing one across threads. RecordingWorker, EventLoggerWorker,
and the GUI thread (object-detection event logging happens inline in
MainWindow._poll) all call into this concurrently -- sqlite3
connections aren't safe to share across threads without either a lock
or check_same_thread=False plus WAL mode, and one-connection-per-call
is simpler to reason about and cheap enough at this write volume
(motion edges + occasional detections + one segment roll per 30 min
per camera, not a hot path like frame decode).
"""

import os
import sqlite3
import threading

DEFAULT_DB_PATH = os.path.join(os.path.expanduser("~"), ".cctv_viewer_events.db")

# Detection-event thumbnails (JPEG bytes already captured by
# object_detector.py's _make_thumbnail, one per fresh detection) live
# here as sibling files named by event id -- same "derive the path
# deterministically from a stable id, no extra DB column" approach
# recording_manager.py uses for segment thumbnails. Beside the
# project's .py files, not the home directory or cwd -- see
# recording_manager.py's FOOTAGE_ROOT for the same reasoning.
_PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
EVENT_THUMBNAILS_ROOT = os.path.join(_PROJECT_DIR, "event_thumbnails")


def event_thumbnail_path(event_id):
    """Deterministic path for one event's thumbnail JPEG. Callers
    check os.path.exists() themselves -- only object-detection events
    have one (motion/alert rows have no captured image), so a missing
    file here is normal, not an error."""
    return os.path.join(EVENT_THUMBNAILS_ROOT, f"event_{event_id}.jpg")


def save_event_thumbnail(event_id, jpeg_bytes):
    """Writes already-JPEG-encoded bytes to this event's thumbnail
    path. object_detector.py's _make_thumbnail already downscales and
    encodes before a DetectionEvent ever reaches main.py's
    _log_detection_events, so this is just a file write, not another
    encode pass. No-op (returns None) if jpeg_bytes is falsy."""
    if not jpeg_bytes:
        return None
    os.makedirs(EVENT_THUMBNAILS_ROOT, exist_ok=True)
    path = event_thumbnail_path(event_id)
    try:
        with open(path, "wb") as f:
            f.write(jpeg_bytes)
        return path
    except OSError as exc:
        print(f"[event_store] couldn't save thumbnail for event {event_id}: {exc}")
        return None


def _delete_event_thumbnails(event_ids):
    """Best-effort cleanup of thumbnail files for events being deleted
    from the DB -- called from every deletion path below so thumbnails
    never outlive the rows that reference them."""
    for event_id in event_ids:
        path = event_thumbnail_path(event_id)
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass

_SCHEMA = """
CREATE TABLE IF NOT EXISTS segments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    camera_id TEXT NOT NULL,
    start_time TEXT NOT NULL,
    end_time TEXT,
    file_path TEXT NOT NULL,
    file_size_bytes INTEGER
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    camera_id TEXT NOT NULL,
    zone_id TEXT,
    detected_at TEXT NOT NULL,
    detection_class TEXT,
    confidence REAL,
    segment_id INTEGER REFERENCES segments(id),
    pushed_to_cloud INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_segments_camera_time ON segments(camera_id, start_time);
CREATE INDEX IF NOT EXISTS idx_events_camera_time ON events(camera_id, detected_at);
CREATE INDEX IF NOT EXISTS idx_events_segment ON events(segment_id);
"""


class EventStore:
    def __init__(self, path=None):
        self.path = path or DEFAULT_DB_PATH
        # Guards schema creation only -- see module docstring for why
        # regular reads/writes don't share a connection/lock.
        self._init_lock = threading.Lock()
        self._ensure_schema()

    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _ensure_schema(self):
        with self._init_lock:
            conn = self._connect()
            try:
                conn.executescript(_SCHEMA)
                conn.commit()
            finally:
                conn.close()

    # ----- segments ------------------------------------------------

    def start_segment(self, camera_id, start_time_iso, file_path):
        """Insert a new open segment row (end_time NULL). Returns the
        new segment's integer id -- callers (RecordingWorker) hold
        onto this so later events from this camera can be tagged with
        it, and so the segment can be closed out later without a
        lookup."""
        conn = self._connect()
        try:
            cur = conn.execute(
                "INSERT INTO segments (camera_id, start_time, file_path) VALUES (?, ?, ?)",
                (camera_id, start_time_iso, file_path),
            )
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()

    def close_segment(self, segment_id, end_time_iso, file_size_bytes):
        """Fill in end_time/file_size_bytes on an already-open segment
        row. Called when a segment rolls to the next file, and when a
        camera's recording stops (app shutdown or camera removed) so
        no segment is left dangling with a NULL end_time under normal
        operation."""
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE segments SET end_time = ?, file_size_bytes = ? WHERE id = ?",
                (end_time_iso, file_size_bytes, segment_id),
            )
            conn.commit()
        finally:
            conn.close()

    def get_segment(self, segment_id):
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT id, camera_id, start_time, end_time, file_path, file_size_bytes "
                "FROM segments WHERE id = ?",
                (segment_id,),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        return {
            "id": row[0], "camera_id": row[1], "start_time": row[2],
            "end_time": row[3], "file_path": row[4], "file_size_bytes": row[5],
        }

    def get_segments(self, camera_id, limit=500):
        """All segments for one camera, newest first -- what the
        Settings -> Recordings section lists. A segment with end_time
        still NULL is currently being written (see start_segment)."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT id, camera_id, start_time, end_time, file_path, file_size_bytes "
                "FROM segments WHERE camera_id = ? ORDER BY start_time DESC LIMIT ?",
                (camera_id, limit),
            ).fetchall()
        finally:
            conn.close()
        return [
            {
                "id": r[0], "camera_id": r[1], "start_time": r[2],
                "end_time": r[3], "file_path": r[4], "file_size_bytes": r[5],
            }
            for r in rows
        ]

    def delete_segment(self, segment_id):
        """Delete one segment row (and any events pointing at it) by
        id -- the manual per-row Delete button in the Recordings
        section, as opposed to delete_segments_older_than's bulk
        retention sweep. Same "return the file_path, let the caller
        touch the filesystem" split of responsibility as that method.
        Returns the file_path that was recorded, or None if no such
        segment exists. Callers are responsible for not calling this
        on a segment that's still open (end_time NULL) -- deleting a
        file a cv2.VideoWriter still has open behaves inconsistently
        across platforms (silently unlinks on POSIX, usually errors on
        Windows), so the Recordings section's Delete button is
        disabled for those rows rather than this method refusing."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT file_path FROM segments WHERE id = ?", (segment_id,)
            ).fetchone()
            if row is None:
                return None
            event_ids = [
                r[0] for r in conn.execute(
                    "SELECT id FROM events WHERE segment_id = ?", (segment_id,)
                ).fetchall()
            ]
            conn.execute("DELETE FROM events WHERE segment_id = ?", (segment_id,))
            conn.execute("DELETE FROM segments WHERE id = ?", (segment_id,))
            conn.commit()
        finally:
            conn.close()
        _delete_event_thumbnails(event_ids)
        return row[0]

    def get_total_bytes_all_cameras(self):
        """Sum of file_size_bytes across every closed segment, for the
        Recordings section's storage summary. Segments still being
        written (file_size_bytes NULL) aren't included -- their size
        isn't in the DB yet, so an accurate live total needs the
        caller to add each currently-recording camera's live file size
        itself (via os.path.getsize()), the same fallback the
        per-camera table already uses for its Size column."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT COALESCE(SUM(file_size_bytes), 0) FROM segments WHERE file_size_bytes IS NOT NULL"
            ).fetchone()
        finally:
            conn.close()
        return row[0] if row else 0

    def find_segment_for_timestamp(self, camera_id, timestamp_iso):
        """Given a camera and an ISO8601 timestamp, return the segment
        row whose [start_time, end_time) window contains it (end_time
        IS NULL is treated as "still open, extends to now" so a lookup
        against the currently-recording segment still resolves). This
        is the Phase 7 checkpoint query: given a detection event, can
        you open the right clip at the right timestamp."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT id, camera_id, start_time, end_time, file_path, file_size_bytes "
                "FROM segments "
                "WHERE camera_id = ? AND start_time <= ? "
                "AND (end_time IS NULL OR end_time > ?) "
                "ORDER BY start_time DESC LIMIT 1",
                (camera_id, timestamp_iso, timestamp_iso),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        return {
            "id": row[0], "camera_id": row[1], "start_time": row[2],
            "end_time": row[3], "file_path": row[4], "file_size_bytes": row[5],
        }

    # ----- events ----------------------------------------------------

    def add_event(self, camera_id, detected_at_iso, detection_class,
                  zone_id=None, confidence=None, segment_id=None):
        """Insert one event row. Returns the new row's integer id --
        callers (main.py's _log_detection_events) need it to name a
        detection's thumbnail file via event_thumbnail_path(). segment_id
        may be None -- e.g. a motion edge that fires in the brief window
        before this camera's first segment has been opened yet; the row
        is still worth keeping, it just won't resolve to a specific
        clip."""
        conn = self._connect()
        try:
            cur = conn.execute(
                "INSERT INTO events "
                "(camera_id, zone_id, detected_at, detection_class, confidence, segment_id) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (camera_id, zone_id, detected_at_iso, detection_class, confidence, segment_id),
            )
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()

    def get_events(self, camera_id=None, since_iso=None, until_iso=None, limit=200):
        """Most recent events, newest first -- optionally filtered by
        camera and/or a [since_iso, until_iso] window (either bound
        can be omitted). Powers settings_panel.py's Events section
        (camera/type/class/time filters, including an absolute custom
        date range) and is also the source EventsSectionPanel's "View
        Clip" button uses alongside find_segment_for_timestamp() to
        actually open a recording."""
        query = (
            "SELECT id, camera_id, zone_id, detected_at, detection_class, "
            "confidence, segment_id, pushed_to_cloud FROM events WHERE 1=1"
        )
        params = []
        if camera_id is not None:
            query += " AND camera_id = ?"
            params.append(camera_id)
        if since_iso is not None:
            query += " AND detected_at >= ?"
            params.append(since_iso)
        if until_iso is not None:
            query += " AND detected_at <= ?"
            params.append(until_iso)
        query += " ORDER BY detected_at DESC LIMIT ?"
        params.append(limit)

        conn = self._connect()
        try:
            rows = conn.execute(query, params).fetchall()
        finally:
            conn.close()
        return [
            {
                "id": r[0], "camera_id": r[1], "zone_id": r[2], "detected_at": r[3],
                "detection_class": r[4], "confidence": r[5], "segment_id": r[6],
                "pushed_to_cloud": r[7],
            }
            for r in rows
        ]

    # ----- retention ---------------------------------------------------

    def delete_segments_older_than(self, cutoff_iso):
        """Delete segment rows (and any events pointing at them) whose
        start_time is older than cutoff_iso. Only closed segments
        (end_time NOT NULL) are eligible -- a segment still being
        written should never be swept regardless of how old its
        start_time is. Does NOT touch the actual video files -- that's
        RecordingManager's job (it needs the file_path from each row
        before the row disappears), so this returns the deleted rows'
        file_paths for the caller to act on rather than deleting files
        itself. Keeps DB cleanup and filesystem cleanup as two
        explicit steps instead of one method silently doing both."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT id, file_path FROM segments WHERE start_time < ? AND end_time IS NOT NULL",
                (cutoff_iso,),
            ).fetchall()
            ids = [r[0] for r in rows]
            event_ids = []
            if ids:
                placeholders = ",".join("?" * len(ids))
                event_ids = [
                    r[0] for r in conn.execute(
                        f"SELECT id FROM events WHERE segment_id IN ({placeholders})", ids
                    ).fetchall()
                ]
                conn.execute(f"DELETE FROM events WHERE segment_id IN ({placeholders})", ids)
                conn.execute(f"DELETE FROM segments WHERE id IN ({placeholders})", ids)
                conn.commit()
        finally:
            conn.close()
        _delete_event_thumbnails(event_ids)
        return [r[1] for r in rows]

    def delete_orphan_events_older_than(self, cutoff_iso):
        """Events with no segment_id (logged before that camera's
        first segment existed, or a motion edge that raced a segment
        roll) never get swept by delete_segments_older_than since they
        don't reference a segment row at all. Cleaned up separately on
        the same retention cutoff so nothing accumulates unbounded --
        including their thumbnail files, if any."""
        conn = self._connect()
        try:
            event_ids = [
                r[0] for r in conn.execute(
                    "SELECT id FROM events WHERE segment_id IS NULL AND detected_at < ?",
                    (cutoff_iso,),
                ).fetchall()
            ]
            conn.execute(
                "DELETE FROM events WHERE segment_id IS NULL AND detected_at < ?",
                (cutoff_iso,),
            )
            conn.commit()
        finally:
            conn.close()
        _delete_event_thumbnails(event_ids)