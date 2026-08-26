"""
All active cameras arranged in a roughly-square grid.

Layout maths lives in core.ui.widgets.grid_layout, which is pure and
GUI-free so it can be tested without a QApplication. This file only
does the Qt wiring.
"""

import time

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from core.ui.constants import DETECTION_BOX_TTL_SECONDS
from core.ui.theme import (
    COLOR_BORDER,
    COLOR_PANEL_BG,
    COLOR_TEXT_MUTED,
)
from core.ui.theme import button_style as _button_style
from core.ui.widgets.camera_tile import CameraTile
from core.ui.widgets.grid_layout import grid_dimensions, tile_positions

# How long a detection's bounding box stays drawn on the live feed
# after it fires, when the optional "Show Boxes" overlay is on. A
# still-active detection refreshes this naturally every cooldown
# period (see object_detector.DETECTION_COOLDOWN_SECONDS); once the
# subject actually leaves, the box just fades out of relevance within
# this window rather than lingering indefinitely.

# Built lazily (first call) and cached -- a QPixmap can't be constructed
# before QApplication exists, so this can't just be a module-level
# constant. Every CameraTile shares the same icon instance rather than
# each one drawing its own; the glyph never changes, only its
# visibility does.


class GridView(QWidget):
    """All active cameras arranged in a roughly-square grid."""

    cameraActivated = pyqtSignal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.tiles = {}  # cam_id -> CameraTile
        self.zones_visible = True
        self.boxes_visible = False  # off by default -- opt-in, see design discussion

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        header = QFrame()
        header.setStyleSheet(f"background-color: {COLOR_PANEL_BG}; border-bottom: 1px solid {COLOR_BORDER};")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(10, 4, 10, 4)
        header_layout.addStretch(1)

        self.boxes_toggle_btn = QPushButton("Show Boxes")
        self.boxes_toggle_btn.setCheckable(True)
        self.boxes_toggle_btn.setChecked(False)
        self.boxes_toggle_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.boxes_toggle_btn.setStyleSheet(_button_style())
        self.boxes_toggle_btn.toggled.connect(self._on_boxes_toggled)
        header_layout.addWidget(self.boxes_toggle_btn)

        self.zones_toggle_btn = QPushButton("Hide Zones")
        self.zones_toggle_btn.setCheckable(True)
        self.zones_toggle_btn.setChecked(True)
        self.zones_toggle_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.zones_toggle_btn.setStyleSheet(_button_style())
        self.zones_toggle_btn.toggled.connect(self._on_zones_toggled)
        header_layout.addWidget(self.zones_toggle_btn)

        outer.addWidget(header, stretch=0)

        grid_container = QWidget()
        self.layout_ = QGridLayout(grid_container)
        self.layout_.setContentsMargins(6, 6, 6, 6)
        self.layout_.setSpacing(6)
        outer.addWidget(grid_container, stretch=1)

        self.empty_label = QLabel("No cameras yet.\nOpen Settings to add one.")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font-size: 14px;")

    def _on_zones_toggled(self, checked):
        self.zones_visible = checked
        self.zones_toggle_btn.setText("Hide Zones" if checked else "Show Zones")
        for tile in self.tiles.values():
            tile.set_zones_visible(checked)

    def _on_boxes_toggled(self, checked):
        self.boxes_visible = checked
        self.boxes_toggle_btn.setText("Hide Boxes" if checked else "Show Boxes")
        for tile in self.tiles.values():
            tile.set_detection_box_visible(checked)

    def rebuild(self, cameras):
        # Clear existing widgets/layout entries.
        while self.layout_.count():
            item = self.layout_.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
        self.tiles = {}

        if not cameras:
            self.layout_.addWidget(self.empty_label, 0, 0)
            return

        rows, cols = grid_dimensions(len(cameras))
        for r in range(rows):
            self.layout_.setRowStretch(r, 1)
        for c in range(cols):
            self.layout_.setColumnStretch(c, 1)

        positions = tile_positions(len(cameras))
        for camera, (r, c) in zip(cameras, positions):
            # show_zone_labels=False: grid tiles are small, outlines
            # only -- labels would just be clutter at thumbnail size.
            tile = CameraTile(camera, clickable=True, show_zone_labels=False)
            tile.set_zones_visible(self.zones_visible)
            tile.set_detection_box_visible(self.boxes_visible)
            tile.doubleClicked.connect(self.cameraActivated.emit)
            self.layout_.addWidget(tile, r, c)
            self.tiles[camera["id"]] = tile

    def update_frames(self, stream_manager, motion_manager=None, detection_manager=None, recording_manager=None):
        for cam_id, tile in self.tiles.items():
            frame = stream_manager.get_frame(cam_id)
            frame_counter = stream_manager.get_frame_counter(cam_id)
            tile.update_frame(frame, frame_counter=frame_counter)
            status, _err = stream_manager.get_status(cam_id)
            tile.set_status(status)

            if recording_manager is not None:
                tile.set_recording_status(recording_manager.get_status(cam_id))

            # Grid tiles only show the name-bar motion icon (whole-frame
            # bool) -- no per-zone outline highlighting here, consistent
            # with show_zone_labels=False above: small thumbnails stay
            # legible, per-zone detail belongs in single view only.
            if motion_manager is not None:
                result = motion_manager.get_result(cam_id)
                tile.set_motion_active(result.motion)

            # Phase 4 quality-of-life, extended for multi-instance
            # tracking: the box overlay now draws every currently
            # active detection at once (get_active_detections), not
            # just the single most recent one -- see VideoLabel.
            # set_detection_boxes's docstring. No TTL math needed here
            # anymore; object_detector.py's presence slots already
            # self-expire (ABSENCE_TIMEOUT_SECONDS) and
            # get_active_detections only ever returns what's still
            # active right now. The caption-bar badge is a separate,
            # smaller indicator ("something was recently seen") and
            # deliberately still shows only the single latest
            # detection -- see main.py's known-limitation note.
            if detection_manager is not None:
                event = detection_manager.get_latest_event(cam_id)
                badge_active = event is not None and (time.time() - event.timestamp) <= DETECTION_BOX_TTL_SECONDS
                tile.set_detection_badge(event.class_name if badge_active else None)

                active_detections = detection_manager.get_active_detections(cam_id)
                boxes = [
                    (bbox, f"{class_name.capitalize()} {confidence * 100:.0f}%")
                    for class_name, confidence, bbox in active_detections
                ]
                tile.set_detection_boxes(boxes)
