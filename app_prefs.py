"""
app_prefs.py

Tiny persisted store for app-level (not per-camera) preferences --
currently just the light/dark theme choice. Deliberately a separate
file from camera_store.py's JSON: camera data and UI preferences are
different concerns with different lifecycles and different schemas,
and keeping them apart means neither has to think about the other.
Same defensive-on-corruption pattern as camera_store.py -- a missing
or unreadable prefs file just falls back to defaults instead of
crashing the app.
"""

import json
import os

DEFAULT_PREFS_PATH = os.path.join(os.path.expanduser("~"), ".cctv_viewer_prefs.json")

DEFAULT_PREFS = {
    "theme": "dark",
}


def _load(path=None):
    path = path or DEFAULT_PREFS_PATH
    if not os.path.exists(path):
        return dict(DEFAULT_PREFS)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return dict(DEFAULT_PREFS)
        merged = dict(DEFAULT_PREFS)
        merged.update(data)
        return merged
    except (json.JSONDecodeError, OSError):
        return dict(DEFAULT_PREFS)


def _save(prefs, path=None):
    path = path or DEFAULT_PREFS_PATH
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(prefs, f, indent=2)
    except OSError:
        pass  # non-fatal -- the preference just won't persist this session


def load_theme(path=None):
    """Returns the saved theme name ('dark' or 'light'), defaulting to
    'dark' if nothing's been saved yet or the file is unreadable."""
    return _load(path).get("theme", "dark")


def save_theme(theme_name, path=None):
    prefs = _load(path)
    prefs["theme"] = theme_name
    _save(prefs, path)