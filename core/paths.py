"""
core.paths

Single source of truth for every on-disk location the app uses.

Before this module, storage paths were scattered across five files with
three different conventions: the camera DB went next to the executable,
the event DB and alert log went to the home directory, and footage and
thumbnails went next to the executable again. Nobody could find half
their data, and `_project_dir()` was copy-pasted (with one subtle
variation) into three modules.

Everything now lives under one root, overridable with the CCTV_DATA_DIR
environment variable so a test run or a second instance can point
somewhere else without touching code.
"""

import os
import sys


def _install_dir():
    """Directory the exe/script actually lives in.

    For a PyInstaller onefile build, sys.executable is the real exe
    location, not the temporary _MEIPASS extraction folder that
    __file__ would resolve to -- so data persists across runs instead
    of vanishing when the temp folder is cleaned up.
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    # core/paths.py -> core/ -> project root (cctv/)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def data_dir():
    """Root for everything this app writes. Created on first access."""
    root = os.environ.get("CCTV_DATA_DIR") or os.path.join(_install_dir(), "data")
    os.makedirs(root, exist_ok=True)
    return root


def _under(*parts):
    path = os.path.join(data_dir(), *parts)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return path


# --- databases -------------------------------------------------------

def cameras_db():
    return _under("cameras.db")

def settings_db():
    return _under("settings.db")

def events_db():
    return _under("events.db")


def prefs_file():
    return _under("prefs.json")


# --- media -----------------------------------------------------------

def footage_root():
    path = os.path.join(data_dir(), "footage")
    os.makedirs(path, exist_ok=True)
    return path


def event_thumbnails_root():
    path = os.path.join(data_dir(), "event_thumbnails")
    os.makedirs(path, exist_ok=True)
    return path


def alert_log():
    return _under("alerts.log")


# --- bundled resources (read-only, ships with the app) ---------------

def model_path(filename="yolov8n.onnx"):
    """Bundled model weights. Uses _MEIPASS when frozen, since these
    are --add-data resources rather than user data."""
    base = getattr(sys, "_MEIPASS", None) or _install_dir()
    return os.path.join(base, "models", filename)


def downloaded_model_path(filename="yolov8n.onnx"):
    """Writable location for an auto-downloaded model, under data_dir()
    rather than next to the exe -- the install dir is often read-only
    or per-user-installer-managed, while data_dir() is guaranteed
    writable and is what CCTV_DATA_DIR already overrides."""
    return _under("models", filename)
