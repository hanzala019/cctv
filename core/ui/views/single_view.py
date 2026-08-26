"""
One camera, full size, with its controls.

Reached by activating a tile in the grid. Shares CameraTile with
GridView rather than reimplementing the caption bar.
"""

import time

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMessageBox,
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
from core.ui.views.zone_editor import ZoneEditorView
from core.ui.widgets.camera_tile import CameraTile

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


class SingleView(QWidget):
    """Full-size view of one camera, with a button to go back to the
    grid and a "Define Zones" toggle that swaps the plain video tile
    out for the interactive ZoneEditorView.
    """

    backRequested = pyqtSignal()

    def __init__(self, app, parent=None):
        super().__init__(parent)
        self.app = app  # gives access to app.store for zone persistence
        self.camera = None
        self.tile = None
        self.zone_editing = False
        self.zones_visible = True
        self.boxes_visible = False  # off by default -- opt-in, see design discussion

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        top_bar = QFrame()
        top_bar.setStyleSheet(f"background-color: {COLOR_PANEL_BG}; border-bottom: 1px solid {COLOR_BORDER};")
        top_bar_layout = QHBoxLayout(top_bar)
        top_bar_layout.setContentsMargins(8, 6, 8, 6)

        back_btn = QPushButton("←  Back to grid")
        back_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        back_btn.setStyleSheet(_button_style())
        back_btn.clicked.connect(self.backRequested.emit)
        top_bar_layout.addWidget(back_btn)
        top_bar_layout.addStretch(1)

        self.zone_hint_label = QLabel(
            "Click to place points, click the first point (or double-click) to close. Esc cancels."
        )
        self.zone_hint_label.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font-size: 11px;")
        self.zone_hint_label.setVisible(False)
        top_bar_layout.addWidget(self.zone_hint_label)
        top_bar_layout.addStretch(1)

        self.boxes_visible_btn = QPushButton("Show Boxes")
        self.boxes_visible_btn.setCheckable(True)
        self.boxes_visible_btn.setChecked(False)
        self.boxes_visible_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.boxes_visible_btn.setStyleSheet(_button_style())
        self.boxes_visible_btn.toggled.connect(self._on_boxes_visible_toggled)
        top_bar_layout.addWidget(self.boxes_visible_btn)

        self.zones_visible_btn = QPushButton("Hide Zones")
        self.zones_visible_btn.setCheckable(True)
        self.zones_visible_btn.setChecked(True)
        self.zones_visible_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.zones_visible_btn.setStyleSheet(_button_style())
        self.zones_visible_btn.toggled.connect(self._on_zones_visible_toggled)
        top_bar_layout.addWidget(self.zones_visible_btn)

        self.zones_btn = QPushButton("Define Zones")
        self.zones_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.zones_btn.setCheckable(True)
        self.zones_btn.setStyleSheet(_button_style())
        self.zones_btn.toggled.connect(self._on_zones_toggled)
        top_bar_layout.addWidget(self.zones_btn)

        outer.addWidget(top_bar, stretch=0)

        self.body = QVBoxLayout()
        self.body.setContentsMargins(6, 6, 6, 6)
        body_container = QWidget()
        body_container.setLayout(self.body)
        outer.addWidget(body_container, stretch=1)

        # Built once, reused across camera switches (cheaper than
        # rebuilding a QGraphicsScene every time), shown only while
        # zone_editing is True.
        self.zone_editor = ZoneEditorView()
        self.zone_editor.zoneFinalized.connect(self._on_zone_finalized)
        self.zone_editor.zoneDeleteRequested.connect(self._on_zone_delete_requested)
        self.zone_editor.zoneModified.connect(self._on_zone_modified)

    def set_camera(self, camera):
        self.camera = camera
        self._rebuild_body()

    def _rebuild_body(self):
        # Clear previous widget from the body layout without destroying
        # self.zone_editor itself, since we reuse it across cameras.
        while self.body.count():
            item = self.body.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)

        if self.zone_editing:
            self.zone_editor.set_camera(self.camera)
            self.zone_editor.set_edit_mode(True)
            self.body.addWidget(self.zone_editor)
        else:
            # show_zone_labels=True: single view has room to show zone
            # names next to their outlines, unlike the small grid tiles.
            self.tile = CameraTile(self.camera, clickable=False, show_zone_labels=True)
            self.tile.set_zones_visible(self.zones_visible)
            self.tile.set_detection_box_visible(self.boxes_visible)
            self.body.addWidget(self.tile)

    def update_frame(self, stream_manager, motion_manager=None, detection_manager=None, recording_manager=None):
        if self.camera is None:
            return
        frame = stream_manager.get_frame(self.camera["id"])
        status, _err = stream_manager.get_status(self.camera["id"])

        if self.zone_editing:
            self.zone_editor.update_frame(frame)
        elif self.tile is not None:
            frame_counter = stream_manager.get_frame_counter(self.camera["id"])
            self.tile.update_frame(frame, frame_counter=frame_counter)
            self.tile.set_status(status)

            if recording_manager is not None:
                self.tile.set_recording_status(recording_manager.get_status(self.camera["id"]))

            # Single view gets the full Phase 3 treatment: the same
            # name-bar icon grid tiles get, plus per-zone outline
            # highlighting (which zone(s) specifically have motion
            # right now) -- there's room here that a small grid
            # thumbnail doesn't have, and it's the more useful view
            # when you're actually looking at one camera.
            if motion_manager is not None:
                result = motion_manager.get_result(self.camera["id"])
                self.tile.set_motion_active(result.motion)
                self.tile.set_zone_motion(
                    zone_id for zone_id, has_motion in result.zones.items() if has_motion
                )

            # Phase 4 quality-of-life, extended for multi-instance
            # tracking: same multi-box overlay + single-latest badge
            # split as GridView -- see that method's comment for the
            # full reasoning.
            if detection_manager is not None:
                event = detection_manager.get_latest_event(self.camera["id"])
                badge_active = event is not None and (time.time() - event.timestamp) <= DETECTION_BOX_TTL_SECONDS
                self.tile.set_detection_badge(event.class_name if badge_active else None)

                active_detections = detection_manager.get_active_detections(self.camera["id"])
                boxes = [
                    (bbox, f"{class_name.capitalize()} {confidence * 100:.0f}%")
                    for class_name, confidence, bbox in active_detections
                ]
                self.tile.set_detection_boxes(boxes)

    # ----- zone mode toggle ---------------------------------------------

    def _on_zones_toggled(self, checked):
        self.zone_editing = checked
        self.zone_hint_label.setVisible(checked)
        self.zones_btn.setText("Done" if checked else "Define Zones")
        # The "Show/Hide Zones" toggle is about the passive overlay --
        # editing mode always shows zones (you need to see them to
        # edit them), so disable that button while editing rather than
        # letting it fight with edit mode.
        self.zones_visible_btn.setEnabled(not checked)
        self._rebuild_body()

    def _on_zones_visible_toggled(self, checked):
        self.zones_visible = checked
        self.zones_visible_btn.setText("Hide Zones" if checked else "Show Zones")
        if not self.zone_editing and self.tile is not None:
            self.tile.set_zones_visible(checked)

    def _on_boxes_visible_toggled(self, checked):
        self.boxes_visible = checked
        self.boxes_visible_btn.setText("Hide Boxes" if checked else "Show Boxes")
        if not self.zone_editing and self.tile is not None:
            self.tile.set_detection_box_visible(checked)

    # ----- zone editor signal handlers -----------------------------------

    def _on_zone_finalized(self, normalized_points):
        if self.camera is None:
            return

        if not self.app.store.can_add_zone(self.camera["id"]):
            QMessageBox.information(
                self,
                "Zone limit reached",
                "You've reached the maximum number of zones for this camera.",
            )
            return

        name, ok = QInputDialog.getText(self, "Name this zone", "Zone name:")
        name = (name or "").strip()
        if not ok or not name:
            return  # user cancelled or left it blank -- drop the draft silently

        try:
            zone = self.app.store.add_zone(self.camera["id"], name, normalized_points)
        except ValueError as exc:
            QMessageBox.critical(self, "Couldn't add zone", str(exc))
            return

        self.zone_editor.add_zone_to_scene(zone["id"], zone["name"], zone["points"])
        self.app.notify_zones_changed(self.camera["id"])

    def _on_zone_delete_requested(self, zone_id):
        if self.camera is None:
            return
        reply = QMessageBox.question(
            self,
            "Remove zone",
            "Remove this zone?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self.app.store.remove_zone(self.camera["id"], zone_id)
        self.zone_editor.remove_zone_from_scene(zone_id)
        self.app.notify_zones_changed(self.camera["id"])

    def _on_zone_modified(self, zone_id, normalized_points):
        if self.camera is None:
            return
        try:
            self.app.store.update_zone(self.camera["id"], zone_id, points=normalized_points)
        except ValueError as exc:
            QMessageBox.critical(self, "Couldn't update zone", str(exc))
        else:
            self.app.notify_zones_changed(self.camera["id"])
