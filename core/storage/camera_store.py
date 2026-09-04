"""
camera_store.py (SQLite edition)

Drop-in replacement for the JSON-backed CameraStore. Same public API,
same method signatures, same return shapes (get_camera/list_cameras
still hand back a plain dict with nested "zones", "alert_rules",
"object_detection_classes", "object_detection_class_confidence" lists/
dicts) -- every other module (main.py, zone_editor.py, settings_panel.py,
motion_detector.py, object_detector.py, alert_manager.py) reads camera
dicts via those same keys, so nothing downstream needs to change.

Why move off the flat JSON file: every single-field edit (toggling
motion_enabled, tweaking one alert rule, nudging one zone point)
previously rewrote the *entire* cameras.json, including every other
camera's full zone/alert-rule/class-confidence data, on every save().
That's fine at a handful of cameras but doesn't scale, and it's a
classic torn-write risk if the app crashes mid-save (partial JSON =
every camera's config lost, not just the one being edited). SQLite
gives per-row updates and atomic commits instead.

Storage boundary convention matches event_store.py: every other module
calls CameraStore's methods and never touches the sqlite3 connection
directly. Same per-call-connection threading model as event_store.py
too -- MotionManager/ObjectDetectionManager/AlertManager workers all
call into this concurrently from background threads, and sqlite3
connections aren't safe to share across threads without either a lock
or check_same_thread=False + WAL, so one connection per call is the
simpler, cheap-enough-at-this-write-volume choice (config reads/writes
are nowhere near a hot path like frame decode).

Schema
------
cameras                  -- one row per camera, the scalar fields
detection_classes        -- object_detection_classes list (camera_id, class_name)
class_confidence         -- object_detection_class_confidence dict (camera_id, class_name, threshold)
alert_rules               -- one row per alert rule
alert_rule_classes        -- a rule's "classes" list (rule_id, class_name)
zones                     -- one row per zone
zone_points                -- a zone's polygon points, ordered by seq

Insertion order (for list_cameras/get_zones/get_alert_rules) is
preserved via each table's implicit rowid -- none of these tables use
WITHOUT ROWID, so "ORDER BY rowid" reproduces the same order the JSON
list/array order used to give for free.
"""

import sqlite3
import threading
import uuid

from core import paths

# Resolved lazily via paths.cameras_db() rather than captured at import
# time, so CCTV_DATA_DIR set by a test or a second instance is honoured.
# Callers that need a different file pass path= to the constructor.

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cameras (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    url TEXT NOT NULL,
    type TEXT NOT NULL DEFAULT 'rtsp',
    motion_enabled INTEGER NOT NULL DEFAULT 1,
    motion_sensitivity TEXT NOT NULL DEFAULT 'medium',
    object_detection_enabled INTEGER NOT NULL DEFAULT 0,
    object_detection_mode TEXT NOT NULL DEFAULT 'on_motion'
);

CREATE TABLE IF NOT EXISTS detection_classes (
    camera_id TEXT NOT NULL REFERENCES cameras(id) ON DELETE CASCADE,
    class_name TEXT NOT NULL,
    PRIMARY KEY (camera_id, class_name)
);

CREATE TABLE IF NOT EXISTS class_confidence (
    camera_id TEXT NOT NULL REFERENCES cameras(id) ON DELETE CASCADE,
    class_name TEXT NOT NULL,
    threshold REAL NOT NULL,
    PRIMARY KEY (camera_id, class_name)
);

CREATE TABLE IF NOT EXISTS alert_rules (
    id TEXT PRIMARY KEY,
    camera_id TEXT NOT NULL REFERENCES cameras(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    start TEXT NOT NULL,
    end TEXT NOT NULL,
    trigger_type TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS alert_rule_classes (
    rule_id TEXT NOT NULL REFERENCES alert_rules(id) ON DELETE CASCADE,
    class_name TEXT NOT NULL,
    PRIMARY KEY (rule_id, class_name)
);

CREATE TABLE IF NOT EXISTS zones (
    id TEXT PRIMARY KEY,
    camera_id TEXT NOT NULL REFERENCES cameras(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    detection_enabled INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS zone_points (
    zone_id TEXT NOT NULL REFERENCES zones(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    x REAL NOT NULL,
    y REAL NOT NULL,
    PRIMARY KEY (zone_id, seq)
);

CREATE INDEX IF NOT EXISTS idx_alert_rules_camera ON alert_rules(camera_id);
CREATE INDEX IF NOT EXISTS idx_zones_camera ON zones(camera_id);
"""


class CameraStore:
    # Same fallback event_store's DEFAULT_CLASS_CONFIDENCE mirrors --
    # kept as its own constant here too, no cross-module import.
    DEFAULT_CLASS_CONFIDENCE = 0.4

    VALID_SENSITIVITIES = ("low", "medium", "high")
    DEFAULT_SENSITIVITY = "medium"

    VALID_DETECTION_MODES = ("on_motion", "continuous")
    DEFAULT_DETECTION_MODE = "on_motion"
    DEFAULT_DETECTION_CLASSES = ["person"]

    VALID_TRIGGER_TYPES = ("motion", "object_class")

    def __init__(self, path=None):
        self.path = path or paths.cameras_db()
        self._init_lock = threading.Lock()
        self._ensure_schema()

    # ----- connection / schema ------------------------------------

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

    # ----- internal: assembling the nested camera dict ---------------

    def _row_to_camera(self, conn, row):
        (cam_id, name, url, cam_type, motion_enabled, motion_sensitivity,
         od_enabled, od_mode) = row

        classes = [
            r[0] for r in conn.execute(
                "SELECT class_name FROM detection_classes WHERE camera_id = ? ORDER BY rowid",
                (cam_id,),
            ).fetchall()
        ]
        confidence = {
            r[0]: r[1] for r in conn.execute(
                "SELECT class_name, threshold FROM class_confidence WHERE camera_id = ?",
                (cam_id,),
            ).fetchall()
        }

        rules = []
        for rule_row in conn.execute(
            "SELECT id, name, enabled, start, end, trigger_type FROM alert_rules "
            "WHERE camera_id = ? ORDER BY rowid",
            (cam_id,),
        ).fetchall():
            rule_id = rule_row[0]
            rule_classes = [
                r[0] for r in conn.execute(
                    "SELECT class_name FROM alert_rule_classes WHERE rule_id = ? ORDER BY rowid",
                    (rule_id,),
                ).fetchall()
            ]
            rules.append({
                "id": rule_id,
                "name": rule_row[1],
                "enabled": bool(rule_row[2]),
                "start": rule_row[3],
                "end": rule_row[4],
                "trigger_type": rule_row[5],
                "classes": rule_classes,
            })

        zones = []
        for zone_row in conn.execute(
            "SELECT id, name, detection_enabled FROM zones WHERE camera_id = ? ORDER BY rowid",
            (cam_id,),
        ).fetchall():
            zone_id = zone_row[0]
            points = [
                [r[0], r[1]] for r in conn.execute(
                    "SELECT x, y FROM zone_points WHERE zone_id = ? ORDER BY seq",
                    (zone_id,),
                ).fetchall()
            ]
            zones.append({
                "id": zone_id,
                "name": zone_row[1],
                "points": points,
                "detection_enabled": bool(zone_row[2]),
            })

        return {
            "id": cam_id,
            "name": name,
            "url": url,
            "type": cam_type,
            "motion_enabled": bool(motion_enabled),
            "motion_sensitivity": motion_sensitivity,
            "object_detection_enabled": bool(od_enabled),
            "object_detection_mode": od_mode,
            "object_detection_classes": classes,
            "object_detection_class_confidence": confidence,
            "alert_rules": rules,
            "zones": zones,
        }

    def _fetch_camera_row(self, conn, cam_id):
        return conn.execute(
            "SELECT id, name, url, type, motion_enabled, motion_sensitivity, "
            "object_detection_enabled, object_detection_mode FROM cameras WHERE id = ?",
            (cam_id,),
        ).fetchone()

    def _require_camera_row(self, conn, cam_id):
        row = self._fetch_camera_row(conn, cam_id)
        if row is None:
            raise ValueError(f"No camera found with id {cam_id}")
        return row

    # ----- camera CRUD -----------------------------------------------

    def list_cameras(self):
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT id, name, url, type, motion_enabled, motion_sensitivity, "
                "object_detection_enabled, object_detection_mode FROM cameras ORDER BY rowid"
            ).fetchall()
            return [self._row_to_camera(conn, r) for r in rows]
        finally:
            conn.close()

    def get_camera(self, cam_id):
        conn = self._connect()
        try:
            row = self._fetch_camera_row(conn, cam_id)
            if row is None:
                return None
            return self._row_to_camera(conn, row)
        finally:
            conn.close()

    def add_camera(self, name, url, cam_type="rtsp"):
        name = (name or "").strip()
        url = (url or "").strip()
        if not name:
            raise ValueError("Camera name cannot be empty.")
        if not url:
            raise ValueError("Camera URL cannot be empty.")

        cam_id = uuid.uuid4().hex[:8]
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO cameras (id, name, url, type) VALUES (?, ?, ?, ?)",
                (cam_id, name, url, cam_type),
            )
            for class_name in self.DEFAULT_DETECTION_CLASSES:
                conn.execute(
                    "INSERT INTO detection_classes (camera_id, class_name) VALUES (?, ?)",
                    (cam_id, class_name),
                )
            conn.commit()
            return self._row_to_camera(conn, self._fetch_camera_row(conn, cam_id))
        finally:
            conn.close()

    def update_camera(self, cam_id, name=None, url=None, cam_type=None):
        conn = self._connect()
        try:
            self._require_camera_row(conn, cam_id)

            if name is not None:
                name = name.strip()
                if not name:
                    raise ValueError("Camera name cannot be empty.")
                conn.execute("UPDATE cameras SET name = ? WHERE id = ?", (name, cam_id))

            if url is not None:
                url = url.strip()
                if not url:
                    raise ValueError("Camera URL cannot be empty.")
                conn.execute("UPDATE cameras SET url = ? WHERE id = ?", (url, cam_id))

            if cam_type is not None:
                conn.execute("UPDATE cameras SET type = ? WHERE id = ?", (cam_type, cam_id))

            conn.commit()
            return self._row_to_camera(conn, self._fetch_camera_row(conn, cam_id))
        finally:
            conn.close()

    def remove_camera(self, cam_id):
        conn = self._connect()
        try:
            cur = conn.execute("DELETE FROM cameras WHERE id = ?", (cam_id,))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    # ----- motion detection settings (Phase 3 tuning) -----------------

    def get_motion_enabled(self, cam_id):
        conn = self._connect()
        try:
            row = self._require_camera_row(conn, cam_id)
            return bool(row[4])
        finally:
            conn.close()

    def set_motion_enabled(self, cam_id, enabled):
        conn = self._connect()
        try:
            self._require_camera_row(conn, cam_id)
            conn.execute(
                "UPDATE cameras SET motion_enabled = ? WHERE id = ?",
                (1 if enabled else 0, cam_id),
            )
            conn.commit()
            return self._row_to_camera(conn, self._fetch_camera_row(conn, cam_id))
        finally:
            conn.close()

    def get_motion_sensitivity(self, cam_id):
        conn = self._connect()
        try:
            row = self._require_camera_row(conn, cam_id)
            return row[5]
        finally:
            conn.close()

    def set_motion_sensitivity(self, cam_id, sensitivity):
        if sensitivity not in self.VALID_SENSITIVITIES:
            raise ValueError(
                f"Sensitivity must be one of {self.VALID_SENSITIVITIES}, got {sensitivity!r}"
            )
        conn = self._connect()
        try:
            self._require_camera_row(conn, cam_id)
            conn.execute(
                "UPDATE cameras SET motion_sensitivity = ? WHERE id = ?", (sensitivity, cam_id)
            )
            conn.commit()
            return self._row_to_camera(conn, self._fetch_camera_row(conn, cam_id))
        finally:
            conn.close()

    # ----- object detection settings (Phase 4 tuning) ------------------

    def get_object_detection_enabled(self, cam_id):
        conn = self._connect()
        try:
            row = self._require_camera_row(conn, cam_id)
            return bool(row[6])
        finally:
            conn.close()

    def set_object_detection_enabled(self, cam_id, enabled):
        conn = self._connect()
        try:
            self._require_camera_row(conn, cam_id)
            conn.execute(
                "UPDATE cameras SET object_detection_enabled = ? WHERE id = ?",
                (1 if enabled else 0, cam_id),
            )
            conn.commit()
            return self._row_to_camera(conn, self._fetch_camera_row(conn, cam_id))
        finally:
            conn.close()

    def get_object_detection_mode(self, cam_id):
        conn = self._connect()
        try:
            row = self._require_camera_row(conn, cam_id)
            return row[7]
        finally:
            conn.close()

    def set_object_detection_mode(self, cam_id, mode):
        if mode not in self.VALID_DETECTION_MODES:
            raise ValueError(f"Mode must be one of {self.VALID_DETECTION_MODES}, got {mode!r}")
        conn = self._connect()
        try:
            self._require_camera_row(conn, cam_id)
            conn.execute("UPDATE cameras SET object_detection_mode = ? WHERE id = ?", (mode, cam_id))
            conn.commit()
            return self._row_to_camera(conn, self._fetch_camera_row(conn, cam_id))
        finally:
            conn.close()

    def get_object_detection_classes(self, cam_id):
        conn = self._connect()
        try:
            self._require_camera_row(conn, cam_id)
            return [
                r[0] for r in conn.execute(
                    "SELECT class_name FROM detection_classes WHERE camera_id = ? ORDER BY rowid",
                    (cam_id,),
                ).fetchall()
            ]
        finally:
            conn.close()

    def set_object_detection_classes(self, cam_id, classes):
        cleaned = [str(c).strip() for c in (classes or []) if str(c).strip()]
        conn = self._connect()
        try:
            self._require_camera_row(conn, cam_id)
            conn.execute("DELETE FROM detection_classes WHERE camera_id = ?", (cam_id,))
            for class_name in cleaned:
                conn.execute(
                    "INSERT INTO detection_classes (camera_id, class_name) VALUES (?, ?)",
                    (cam_id, class_name),
                )
            conn.commit()
            return self._row_to_camera(conn, self._fetch_camera_row(conn, cam_id))
        finally:
            conn.close()

    # ----- per-class confidence thresholds (multi-instance QoL) --------

    def get_class_confidence_thresholds(self, cam_id):
        conn = self._connect()
        try:
            self._require_camera_row(conn, cam_id)
            return {
                r[0]: r[1] for r in conn.execute(
                    "SELECT class_name, threshold FROM class_confidence WHERE camera_id = ?",
                    (cam_id,),
                ).fetchall()
            }
        finally:
            conn.close()

    def get_class_confidence(self, cam_id, class_name):
        conn = self._connect()
        try:
            self._require_camera_row(conn, cam_id)
            row = conn.execute(
                "SELECT threshold FROM class_confidence WHERE camera_id = ? AND class_name = ?",
                (cam_id, class_name),
            ).fetchone()
            return float(row[0]) if row is not None else self.DEFAULT_CLASS_CONFIDENCE
        finally:
            conn.close()

    def set_class_confidence_threshold(self, cam_id, class_name, threshold):
        threshold = float(threshold)
        if not (0.0 <= threshold <= 1.0):
            raise ValueError(f"Confidence threshold must be between 0.0 and 1.0, got {threshold!r}")
        conn = self._connect()
        try:
            self._require_camera_row(conn, cam_id)
            conn.execute(
                "INSERT INTO class_confidence (camera_id, class_name, threshold) VALUES (?, ?, ?) "
                "ON CONFLICT(camera_id, class_name) DO UPDATE SET threshold = excluded.threshold",
                (cam_id, class_name, threshold),
            )
            conn.commit()
            return self._row_to_camera(conn, self._fetch_camera_row(conn, cam_id))
        finally:
            conn.close()

    # ----- alert rules (Phase 5) ---------------------------------------

    def can_use_object_class_trigger(self, cam_id):
        return True

    def get_alert_rules(self, cam_id):
        conn = self._connect()
        try:
            self._require_camera_row(conn, cam_id)
            return self._row_to_camera(conn, self._fetch_camera_row(conn, cam_id))["alert_rules"]
        finally:
            conn.close()

    def get_alert_rule(self, cam_id, rule_id):
        for rule in self.get_alert_rules(cam_id):
            if rule["id"] == rule_id:
                return rule
        return None

    @staticmethod
    def _validate_hhmm(value, field_label):
        value = (value or "").strip()
        parts = value.split(":")
        if len(parts) != 2:
            raise ValueError(f"{field_label} must be in HH:MM format, got {value!r}")
        try:
            h, m = int(parts[0]), int(parts[1])
        except ValueError:
            raise ValueError(f"{field_label} must be in HH:MM format, got {value!r}")
        if not (0 <= h <= 23 and 0 <= m <= 59):
            raise ValueError(f"{field_label} must be a valid 24h time, got {value!r}")
        return f"{h:02d}:{m:02d}"

    def add_alert_rule(self, cam_id, name, start, end, trigger_type, classes=None, enabled=True):
        name = (name or "").strip()
        if not name:
            raise ValueError("Rule name cannot be empty.")
        if trigger_type not in self.VALID_TRIGGER_TYPES:
            raise ValueError(
                f"trigger_type must be one of {self.VALID_TRIGGER_TYPES}, got {trigger_type!r}"
            )
        if trigger_type == "object_class" and not self.can_use_object_class_trigger(cam_id):
            raise ValueError("Object-class alert triggers aren't available for this camera.")

        start = self._validate_hhmm(start, "Start time")
        end = self._validate_hhmm(end, "End time")
        cleaned_classes = [str(c).strip() for c in (classes or []) if str(c).strip()]

        conn = self._connect()
        try:
            self._require_camera_row(conn, cam_id)
            rule_id = uuid.uuid4().hex[:8]
            conn.execute(
                "INSERT INTO alert_rules (id, camera_id, name, enabled, start, end, trigger_type) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (rule_id, cam_id, name, 1 if enabled else 0, start, end, trigger_type),
            )
            for class_name in cleaned_classes:
                conn.execute(
                    "INSERT INTO alert_rule_classes (rule_id, class_name) VALUES (?, ?)",
                    (rule_id, class_name),
                )
            conn.commit()
            return self.get_alert_rule(cam_id, rule_id)
        finally:
            conn.close()

    def update_alert_rule(self, cam_id, rule_id, name=None, start=None, end=None,
                           trigger_type=None, classes=None, enabled=None):
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT id FROM alert_rules WHERE id = ? AND camera_id = ?", (rule_id, cam_id)
            ).fetchone()
            if row is None:
                raise ValueError(f"No alert rule found with id {rule_id}")

            if name is not None:
                name = name.strip()
                if not name:
                    raise ValueError("Rule name cannot be empty.")
                conn.execute("UPDATE alert_rules SET name = ? WHERE id = ?", (name, rule_id))

            if trigger_type is not None:
                if trigger_type not in self.VALID_TRIGGER_TYPES:
                    raise ValueError(
                        f"trigger_type must be one of {self.VALID_TRIGGER_TYPES}, got {trigger_type!r}"
                    )
                if trigger_type == "object_class" and not self.can_use_object_class_trigger(cam_id):
                    raise ValueError("Object-class alert triggers aren't available for this camera.")
                conn.execute(
                    "UPDATE alert_rules SET trigger_type = ? WHERE id = ?", (trigger_type, rule_id)
                )

            if start is not None:
                conn.execute(
                    "UPDATE alert_rules SET start = ? WHERE id = ?",
                    (self._validate_hhmm(start, "Start time"), rule_id),
                )
            if end is not None:
                conn.execute(
                    "UPDATE alert_rules SET end = ? WHERE id = ?",
                    (self._validate_hhmm(end, "End time"), rule_id),
                )

            if classes is not None:
                conn.execute("DELETE FROM alert_rule_classes WHERE rule_id = ?", (rule_id,))
                for class_name in classes:
                    class_name = str(class_name).strip()
                    if class_name:
                        conn.execute(
                            "INSERT INTO alert_rule_classes (rule_id, class_name) VALUES (?, ?)",
                            (rule_id, class_name),
                        )

            if enabled is not None:
                conn.execute(
                    "UPDATE alert_rules SET enabled = ? WHERE id = ?",
                    (1 if enabled else 0, rule_id),
                )

            conn.commit()
            return self.get_alert_rule(cam_id, rule_id)
        finally:
            conn.close()

    def remove_alert_rule(self, cam_id, rule_id):
        conn = self._connect()
        try:
            self._require_camera_row(conn, cam_id)
            cur = conn.execute(
                "DELETE FROM alert_rules WHERE id = ? AND camera_id = ?", (rule_id, cam_id)
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    # ----- zones --------------------------------------------------------

    def get_zones(self, cam_id):
        conn = self._connect()
        try:
            self._require_camera_row(conn, cam_id)
            return self._row_to_camera(conn, self._fetch_camera_row(conn, cam_id))["zones"]
        finally:
            conn.close()

    def get_zone(self, cam_id, zone_id):
        for zone in self.get_zones(cam_id):
            if zone["id"] == zone_id:
                return zone
        return None

    def can_add_zone(self, cam_id):
        """Same future tier-limit hook as the JSON version -- always
        True today, centralized here for a later plan/limit check."""
        return True

    @staticmethod
    def _validate_points(points):
        points = [(float(x), float(y)) for x, y in points]
        if len(points) < 3:
            raise ValueError("A zone needs at least 3 points.")
        for x, y in points:
            if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
                raise ValueError("Zone points must be normalized between 0.0 and 1.0.")
        return points

    def add_zone(self, cam_id, name, points):
        name = (name or "").strip()
        if not name:
            raise ValueError("Zone name cannot be empty.")
        points = self._validate_points(points)

        conn = self._connect()
        try:
            self._require_camera_row(conn, cam_id)
            if not self.can_add_zone(cam_id):
                raise ValueError("Zone limit reached for this camera.")

            zone_id = uuid.uuid4().hex[:8]
            conn.execute(
                "INSERT INTO zones (id, camera_id, name, detection_enabled) VALUES (?, ?, ?, 0)",
                (zone_id, cam_id, name),
            )
            for seq, (x, y) in enumerate(points):
                conn.execute(
                    "INSERT INTO zone_points (zone_id, seq, x, y) VALUES (?, ?, ?, ?)",
                    (zone_id, seq, x, y),
                )
            conn.commit()
            return self.get_zone(cam_id, zone_id)
        finally:
            conn.close()

    def update_zone(self, cam_id, zone_id, name=None, points=None, detection_enabled=None):
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT id FROM zones WHERE id = ? AND camera_id = ?", (zone_id, cam_id)
            ).fetchone()
            if row is None:
                raise ValueError(f"No zone found with id {zone_id}")

            if name is not None:
                name = name.strip()
                if not name:
                    raise ValueError("Zone name cannot be empty.")
                conn.execute("UPDATE zones SET name = ? WHERE id = ?", (name, zone_id))

            if points is not None:
                validated = self._validate_points(points)
                conn.execute("DELETE FROM zone_points WHERE zone_id = ?", (zone_id,))
                for seq, (x, y) in enumerate(validated):
                    conn.execute(
                        "INSERT INTO zone_points (zone_id, seq, x, y) VALUES (?, ?, ?, ?)",
                        (zone_id, seq, x, y),
                    )

            if detection_enabled is not None:
                conn.execute(
                    "UPDATE zones SET detection_enabled = ? WHERE id = ?",
                    (1 if detection_enabled else 0, zone_id),
                )

            conn.commit()
            return self.get_zone(cam_id, zone_id)
        finally:
            conn.close()

    def remove_zone(self, cam_id, zone_id):
        conn = self._connect()
        try:
            self._require_camera_row(conn, cam_id)
            cur = conn.execute(
                "DELETE FROM zones WHERE id = ? AND camera_id = ?", (zone_id, cam_id)
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()
