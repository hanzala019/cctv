"""
settings_store.py

App-level storage settings -- currently just where recordings are
written. Kept out of event_store.py because config and event data
have different lifecycles (one row vs. thousands), the same split
app_prefs.py makes for theme.

Own DB file rather than a table in events.db, so neither store has
to think about the other's schema. Per-call connections, same
threading model as the other stores.
"""

import os
import shutil
import sqlite3
import threading

from core import paths

_SCHEMA = """
CREATE TABLE IF NOT EXISTS storage_settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    recording_path TEXT,
    duration_minutes REAL NOT NULL DEFAULT 30.0,
    max_storage_gb REAL NOT NULL DEFAULT 50.0,  -- 50 GB
    max_recording_gb REAL NOT NULL DEFAULT 10.0,  -- 10 GB
    max_event_thumbnails_gb REAL NOT NULL DEFAULT 1.0,  -- 1 GB
    max_clip_gb REAL NOT NULL DEFAULT 2.5,  -- 2.5 GB
    retention_days INTEGER NOT NULL DEFAULT 14,
    last_retention_sweep TEXT
    );
"""

DEFAULTS = {
    "recording_path": None,
    "duration_minutes": 30.0,
    "max_storage_gb": 50.0,
    "max_recording_gb": 10.0,
    "max_event_thumbnails_gb": 1.0,
    "max_clip_gb": 2.5,
    "retention_days": 14,
    "last_retention_sweep": None,
}

GB = 1024 ** 3 # basically 1024*1024*1024

def gb_to_bytes(gb):
    return int(gb * GB)

def format_bytes(bytes_num):
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if bytes_num < 1024:
            formatted = f"{bytes_num:.1f}".rstrip('0').rstrip('.')
            return f"{formatted}{unit}"
        bytes_num /= 1024
    return f"{bytes_num:.1f}PB"

class StorageSettingsStore:
    def __init__(self, path=None):
        self.path = path or paths.settings_db()
        self._init_lock = threading.Lock()
        self._ensure_schema()

    def _connect(self):
        return sqlite3.connect(self.path, timeout=10)

    def _ensure_schema(self):
        with self._init_lock:
            conn = self._connect()
            try:
                conn.executescript(_SCHEMA)
                conn.commit()
            finally:
                conn.close()

    _SETTABLE = {
        "duration_minutes", "max_storage_gb", "max_recording_gb",
        "max_event_thumbnails_gb", "max_clip_gb", "retention_days",
        "last_retention_sweep",
    }

    def get_settings_info(self):
        """Always returns a full dict. On a fresh install no row exists
        yet, so the schema defaults are returned rather than {} --
        callers never have to handle two shapes."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT recording_path, duration_minutes, max_storage_gb, "
                "max_recording_gb, max_event_thumbnails_gb, max_clip_gb, "
                "retention_days, last_retention_sweep FROM storage_settings WHERE id = 1"
            ).fetchone()
            if row is None:
                return dict(DEFAULTS)
            return dict(zip(DEFAULTS.keys(), row))
        finally:
            conn.close()


    def get_disk_space(self, path):
        """Return a dict with total, used, and free space formatted as human-readable strings."""
        if not os.path.exists(path):
            raise ValueError(f"Path does not exist: {path}")
        usage = shutil.disk_usage(path)
        return {
            "total": format_bytes(usage.total),
            "used": format_bytes(usage.used),
            "free": format_bytes(usage.free),
        }

    def get_disk_space_bytes(self, path=None):
        """ get disk space in bytes"""
        path = path or self.get_effective_recording_path()
        if not os.path.exists(path):
            raise ValueError(f"Path does not exist: {path}")
        usage = shutil.disk_usage(path)
        return {"total": usage.total, "used": usage.used, "free": usage.free}

    def get_recording_path(self):
        """The user-chosen recordings root, or None to use the default."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT recording_path FROM storage_settings WHERE id = 1"
            ).fetchone()
            return row[0] if row else None
        finally:
            conn.close()

    def get_effective_recording_path(self):
        return self.get_recording_path() or paths.footage_root()

    def set_recording_path(self, path):
        """Passing None clears the setting and restores the default.

        Validated here rather than at write time: a bad path should
        fail in the settings dialog, not silently four hours later
        when the next segment tries to open.
        """
        if path is not None:
            path = os.path.abspath(os.path.expanduser(path))
            try:
                os.makedirs(path, exist_ok=True)
                probe = os.path.join(path, ".write_test")
                with open(probe, "w") as f:
                    f.write("")
                os.remove(probe)
            except OSError as e:
                raise ValueError(f"recording_path is not writable: {e}")

        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO storage_settings (id, recording_path) VALUES (1, ?) "
                "ON CONFLICT(id) DO UPDATE SET recording_path = excluded.recording_path",
                (path,),
            )
            conn.commit()
        finally:
            conn.close()

    def _set_field(self, column, value):
        """Column is whitelisted because it's interpolated into the SQL --
        SQLite can't parameterise identifiers, only values."""
        if column not in self._SETTABLE:
            raise ValueError(f"unknown setting: {column}")
        conn = self._connect()
        try:
            conn.execute(
                f"INSERT INTO storage_settings (id, {column}) VALUES (1, ?) "
                f"ON CONFLICT(id) DO UPDATE SET {column} = excluded.{column}",
                (value,),
            )
            conn.commit()
        finally:
            conn.close()

    def set_duration_minutes(self, minutes):
        minutes = float(minutes)
        if minutes <= 0:
            raise ValueError("duration_minutes must be greater than 0")
        self._set_field("duration_minutes", minutes)

    def set_retention_days(self, days):
        """Lowering this deletes footage on the next hourly sweep --
        the UI should confirm before calling it."""
        days = int(days)
        if days < 1:
            raise ValueError("retention_days must be at least 1")
        self._set_field("retention_days", days)

    def _validated_quota_gb(self, gb, name):
        """A quota above currently-free space is allowed -- that space
        may be held by footage we're about to evict. Only a quota
        above the volume's total capacity is impossible."""
        gb = float(gb)
        if gb <= 0:
            raise ValueError(f"{name} must be greater than 0")
        total = self.get_disk_space_bytes()["total"]
        if gb_to_bytes(gb) > total:
            raise ValueError(
                f"{name} ({gb} GB) exceeds the volume's capacity "
                f"({format_bytes(total)})"
            )
        return gb

    def set_max_storage_gb(self, gb):
        self._set_field("max_storage_gb", self._validated_quota_gb(gb, "max_storage_gb"))
