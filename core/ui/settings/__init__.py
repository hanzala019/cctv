"""
core.ui.settings

The Settings view, split one module per section so several people can
work on different sections without touching the same file.

Re-exported here so callers keep a single import site:

    from core.ui.settings import SettingsPanel

Adding a section means a new module, one line in panel.py, and one line
here. Never edit another section's file to add yours.
"""

from core.ui.settings.alerts import AlertsSectionPanel
from core.ui.settings.cameras import CamerasSectionPanel
from core.ui.settings.events import EventsSectionPanel
from core.ui.settings.object_detection import ObjectDetectionSectionPanel
from core.ui.settings.panel import SettingsPanel
from core.ui.settings.recordings import RecordingsSectionPanel
from core.ui.settings.zones import ZonesSectionPanel

__all__ = [
    "AlertsSectionPanel",
    "CamerasSectionPanel",
    "EventsSectionPanel",
    "ObjectDetectionSectionPanel",
    "RecordingsSectionPanel",
    "SettingsPanel",
    "ZonesSectionPanel",
]
