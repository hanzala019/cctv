"""
migrate_cameras_to_sqlite.py

One-time migration: reads the old cctv_viewer_cameras.json (the flat
file the JSON CameraStore used) and writes every camera into the new
SQLite-backed store (camera_store.py in this same folder), preserving
ids so nothing else (footage folder names, event_store camera_id
references) needs to change.

Usage:
    python migrate_cameras_to_sqlite.py [path/to/cctv_viewer_cameras.json]

If no path is given, looks for cctv_viewer_cameras.json next to this
script (the JSON store's default location). Writes to
cctv_viewer_cameras.db next to this script (the new store's default
location) unless one already exists there, in which case it refuses to
overwrite -- run against a fresh location or delete the old .db first.
"""

import json
import os
import sys

from camera_store import CameraStore, DEFAULT_STORE_PATH


def _default_json_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "cctv_viewer_cameras.json")


def migrate(json_path, db_path):
    if os.path.exists(db_path):
        raise SystemExit(
            f"Refusing to migrate: {db_path} already exists. "
            f"Move it aside first if you want to re-run the migration."
        )

    with open(json_path, "r", encoding="utf-8") as f:
        cameras = json.load(f)

    if not isinstance(cameras, list):
        raise SystemExit(f"{json_path} doesn't look like a camera list -- aborting.")

    store = CameraStore(path=db_path)
    conn = store._connect()  # noqa: SLF001 -- migration script, direct access is fine here
    try:
        for cam in cameras:
            conn.execute(
                "INSERT INTO cameras (id, name, url, type, motion_enabled, motion_sensitivity, "
                "object_detection_enabled, object_detection_mode) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    cam["id"],
                    cam.get("name", ""),
                    cam.get("url", ""),
                    cam.get("type", "rtsp"),
                    1 if cam.get("motion_enabled", True) else 0,
                    cam.get("motion_sensitivity", CameraStore.DEFAULT_SENSITIVITY),
                    1 if cam.get("object_detection_enabled", False) else 0,
                    cam.get("object_detection_mode", CameraStore.DEFAULT_DETECTION_MODE),
                ),
            )

            for class_name in cam.get("object_detection_classes", CameraStore.DEFAULT_DETECTION_CLASSES):
                conn.execute(
                    "INSERT OR IGNORE INTO detection_classes (camera_id, class_name) VALUES (?, ?)",
                    (cam["id"], class_name),
                )

            for class_name, threshold in cam.get("object_detection_class_confidence", {}).items():
                conn.execute(
                    "INSERT INTO class_confidence (camera_id, class_name, threshold) VALUES (?, ?, ?)",
                    (cam["id"], class_name, float(threshold)),
                )

            for rule in cam.get("alert_rules", []):
                conn.execute(
                    "INSERT INTO alert_rules (id, camera_id, name, enabled, start, end, trigger_type) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        rule["id"], cam["id"], rule.get("name", ""),
                        1 if rule.get("enabled", True) else 0,
                        rule.get("start", "00:00"), rule.get("end", "00:00"),
                        rule.get("trigger_type", "motion"),
                    ),
                )
                for class_name in rule.get("classes", []):
                    conn.execute(
                        "INSERT INTO alert_rule_classes (rule_id, class_name) VALUES (?, ?)",
                        (rule["id"], class_name),
                    )

            for zone in cam.get("zones", []):
                conn.execute(
                    "INSERT INTO zones (id, camera_id, name, detection_enabled) VALUES (?, ?, ?, ?)",
                    (zone["id"], cam["id"], zone.get("name", ""),
                     1 if zone.get("detection_enabled", False) else 0),
                )
                for seq, point in enumerate(zone.get("points", [])):
                    x, y = point
                    conn.execute(
                        "INSERT INTO zone_points (zone_id, seq, x, y) VALUES (?, ?, ?, ?)",
                        (zone["id"], seq, float(x), float(y)),
                    )

        conn.commit()
    finally:
        conn.close()

    print(f"Migrated {len(cameras)} camera(s) from {json_path} -> {db_path}")


if __name__ == "__main__":
    json_path = sys.argv[1] if len(sys.argv) > 1 else _default_json_path()
    if not os.path.exists(json_path):
        raise SystemExit(f"No JSON camera file found at {json_path}")
    migrate(json_path, DEFAULT_STORE_PATH)