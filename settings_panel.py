"""
settings_panel.py

Modular settings shell: a left sidebar listing sections (Cameras,
Zones, Object Detection, ...) and a right-hand stacked area showing
whichever section is selected. Built as a full-page QWidget (not a
QDialog) so MainWindow can swap it into its view stack the same way it
swaps Grid/Single view.

Why this shape: each section is its own self-contained QWidget
subclass that only needs to know about `app` (the MainWindow instance,
for store access + signals) and its own data. Adding a new section
later (Alerts, Account, ...) means writing one new panel class and
adding one line to SettingsPanel._build_sections() -- no changes
needed to existing sections, the sidebar, or the stack wiring. This
also makes CRUD/filtering per-section easy to extend independently
(e.g. the Zones section can grow a per-camera filter or search box
without touching the Cameras section at all).

Sections in this file:
    - CamerasSectionPanel: add/edit/remove cameras, jump to single view
    - ZonesSectionPanel: pick a camera, list/rename/delete its zones
    - ObjectDetectionSectionPanel: per-camera Phase 4 tuning -- master
      switch, on-motion/continuous trigger mode, class allowlist
    - AlertsSectionPanel: per-camera Phase 5 time-of-day alert rules
    - RecordingsSectionPanel: per-camera Phase 7 recorded-segment
      browser -- read-only list of what RecordingManager has written,
      with a Play action that opens a clip in the OS's default video
      player (no in-app player, see the Phase 7 design discussion)

Live-sync note: section panels never touch CameraTile/VideoLabel/
ZoneEditorView directly. They mutate app.store and then call
app.notify_zones_changed(cam_id) (for zones) or rely on
app.add_camera/update_camera/remove_camera (for cameras), which already
notify the relevant live views. This keeps this file decoupled from
the view-rendering code in main.py. ObjectDetectionSectionPanel is
simpler still -- object_detector.py reads camera settings fresh from
the store on every tick, so there's no cache to invalidate the way
Phase 3's motion mask cache needed notify_zones_changed for.
RecordingsSectionPanel is simpler again -- it's read-only, querying
app.event_store directly with no mutation path at all.
"""

import os
from datetime import datetime, timedelta

from PyQt6.QtCore import Qt, QTime, QUrl, QDateTime
from PyQt6.QtGui import QDesktopServices, QPixmap
from PyQt6.QtWidgets import (
    QWidget,
    QDialog,
    QMessageBox,
    QVBoxLayout,
    QHBoxLayout,
    QListWidget,
    QListWidgetItem,
    QStackedWidget,
    QLabel,
    QPushButton,
    QLineEdit,
    QComboBox,
    QCheckBox,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QHeaderView,
    QAbstractItemView,
    QFormLayout,
    QFrame,
    QInputDialog,
    QTimeEdit,
    QDateTimeEdit,
)

from ui_theme import (
    COLOR_BG,
    COLOR_PANEL_BG,
    COLOR_CAPTION_BG,
    COLOR_TEXT_PRIMARY,
    COLOR_TEXT_MUTED,
    COLOR_BORDER,
    COLOR_ACCENT,
    COLOR_STATUS_CONNECTING,
    button_style,
    input_style,
    table_style,
    sidebar_style,
)
from zone_editor import ZoneEditorView
from recording_manager import thumbnail_path_for
from event_store import event_thumbnail_path

# Shared time-range filter options for the Recordings and Events
# sections -- same shape as DetectionSidePanel's TIME_RANGE_OPTIONS in
# main.py, kept as a separate copy here rather than importing across
# files for one small list, since the two panels' filtering mechanics
# differ slightly (Events passes since_iso/until_iso straight to
# EventStore.get_events; Recordings filters segments client-side,
# since EventStore.get_segments has no since/until parameters).
# "custom" is a sentinel handled entirely by each panel itself (reads
# its own From/To QDateTimeEdit widgets) rather than by
# _compute_since_iso below, which only knows about the fixed presets.
TIME_RANGE_OPTIONS = [
    ("All time", None),
    ("Last 5 min", 5 * 60),
    ("Last 15 min", 15 * 60),
    ("Last 1 hour", 60 * 60),
    ("Today", "today"),
    ("Custom range…", "custom"),
]

THUMBNAIL_CELL_W = 80
THUMBNAIL_CELL_H = 45


def _compute_since_iso(cutoff_spec):
    """cutoff_spec: None (no cutoff), "today" (local midnight), or an
    int number of seconds to look back. Returns an ISO8601 string
    comparable against detected_at/start_time columns (same local,
    timezone-naive convention every writer in this app already uses),
    or None for "no cutoff". Does NOT handle "custom" -- callers check
    for that sentinel themselves and read their own date/time widgets
    instead (see TIME_RANGE_OPTIONS's comment)."""
    if cutoff_spec is None:
        return None
    if cutoff_spec == "today":
        midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        return midnight.isoformat(timespec="seconds")
    return (datetime.now() - timedelta(seconds=cutoff_spec)).isoformat(timespec="seconds")


def _qdatetime_to_iso(qdatetime):
    """QDateTime (from a QDateTimeEdit) -> the same local,
    timezone-naive ISO8601 string convention every writer in this app
    already uses, so a custom range compares correctly against
    detected_at/start_time columns."""
    return qdatetime.toPyDateTime().isoformat(timespec="seconds")


def _build_time_range_row(parent, on_changed):
    """Builds a (row_layout, time_picker, custom_range_container,
    from_edit, to_edit) tuple -- the preset dropdown plus a From/To
    QDateTimeEdit pair that only appears when "Custom range…" is
    selected. Shared by RecordingsSectionPanel and EventsSectionPanel
    so the two don't duplicate this widget wiring. on_changed is
    called whenever the preset OR either date/time value changes, and
    is also responsible for toggling the container's visibility (the
    caller already has an _on_filter_changed it wants this routed
    through)."""
    row = QHBoxLayout()
    label = QLabel("Time:")
    label.setStyleSheet(f"color: {COLOR_TEXT_PRIMARY};")
    row.addWidget(label)

    time_picker = QComboBox()
    time_picker.setStyleSheet(input_style())
    for option_label, _spec in TIME_RANGE_OPTIONS:
        time_picker.addItem(option_label)
    row.addWidget(time_picker, stretch=1)

    from_label = QLabel("From:")
    from_label.setStyleSheet(f"color: {COLOR_TEXT_PRIMARY};")
    row.addWidget(from_label)
    from_edit = QDateTimeEdit()
    from_edit.setCalendarPopup(True)
    from_edit.setDisplayFormat("yyyy-MM-dd HH:mm")
    from_edit.setDateTime(QDateTime.currentDateTime().addDays(-1))
    from_edit.setStyleSheet(input_style())
    row.addWidget(from_edit)

    to_label = QLabel("To:")
    to_label.setStyleSheet(f"color: {COLOR_TEXT_PRIMARY};")
    row.addWidget(to_label)
    to_edit = QDateTimeEdit()
    to_edit.setCalendarPopup(True)
    to_edit.setDisplayFormat("yyyy-MM-dd HH:mm")
    to_edit.setDateTime(QDateTime.currentDateTime())
    to_edit.setStyleSheet(input_style())
    row.addWidget(to_edit)

    custom_widgets = [from_label, from_edit, to_label, to_edit]

    def _on_preset_changed(index):
        is_custom = TIME_RANGE_OPTIONS[index][1] == "custom"
        for w in custom_widgets:
            w.setVisible(is_custom)
        on_changed()

    for w in custom_widgets:
        w.setVisible(False)

    time_picker.currentIndexChanged.connect(_on_preset_changed)
    from_edit.dateTimeChanged.connect(lambda _dt: on_changed())
    to_edit.dateTimeChanged.connect(lambda _dt: on_changed())

    return row, time_picker, from_edit, to_edit


def _resolve_time_range(time_picker, from_edit, to_edit):
    """Returns (since_iso, until_iso) for whichever option is
    currently selected in a row built by _build_time_range_row --
    either the fixed preset (until_iso always None) or the custom
    From/To pair."""
    _, cutoff_spec = TIME_RANGE_OPTIONS[time_picker.currentIndex()]
    if cutoff_spec == "custom":
        return _qdatetime_to_iso(from_edit.dateTime()), _qdatetime_to_iso(to_edit.dateTime())
    return _compute_since_iso(cutoff_spec), None


class _ImagePreviewDialog(QDialog):
    """Simple modal showing one thumbnail at a larger, legible size.
    The in-table thumbnails are deliberately small (THUMBNAIL_CELL_W x
    THUMBNAIL_CELL_H) to keep rows compact -- this is where "actually
    look at it" happens instead. Source images are already downscaled
    (240px for segments, 160px for detections -- see
    recording_manager.py / object_detector.py), so this caps its
    display size rather than blowing them up past legibility."""

    def __init__(self, image_path, title, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setStyleSheet(f"background-color: {COLOR_PANEL_BG}; color: {COLOR_TEXT_PRIMARY};")

        layout = QVBoxLayout(self)

        label = QLabel()
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        pixmap = QPixmap(image_path) if image_path else QPixmap()
        if not pixmap.isNull():
            scaled = pixmap.scaled(
                640, 480, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation,
            )
            label.setPixmap(scaled)
        else:
            label.setText("Couldn't load this image.")
            label.setStyleSheet(f"color: {COLOR_TEXT_MUTED};")
        layout.addWidget(label)

        close_btn = QPushButton("Close")
        close_btn.setStyleSheet(button_style())
        close_btn.clicked.connect(self.accept)
        layout.addWidget(close_btn, alignment=Qt.AlignmentFlag.AlignRight)


class _ClickableThumbnailLabel(QLabel):
    """QLabel used for table thumbnail cells that opens a larger
    preview on click -- only when it actually has an image; the "No
    preview" placeholder state isn't clickable, matching how every
    other disabled-looking control in this file behaves."""

    def __init__(self, image_path, title, parent_widget):
        super().__init__(parent_widget)
        self.image_path = image_path
        self.title = title
        self.parent_widget = parent_widget
        if image_path:
            self.setCursor(Qt.CursorShape.PointingHandCursor)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.image_path:
            _ImagePreviewDialog(self.image_path, self.title, parent=self.parent_widget).exec()
            return
        super().mousePressEvent(event)


def _make_thumbnail_label(image_path, parent_widget=None, title="Preview"):
    """A small fixed-size clickable thumbnail, or a muted "No preview"
    placeholder if image_path is None/missing -- shared by
    RecordingsSectionPanel (segment thumbnails, always present once a
    segment has at least one frame) and EventsSectionPanel (detection
    thumbnails only -- motion/alert rows never have one, see
    event_store.py's event_thumbnail_path docstring). Click opens
    _ImagePreviewDialog at a larger size -- see that class."""
    has_image = bool(image_path and os.path.exists(image_path))
    label = _ClickableThumbnailLabel(image_path if has_image else None, title, parent_widget)
    label.setFixedSize(THUMBNAIL_CELL_W, THUMBNAIL_CELL_H)
    label.setAlignment(Qt.AlignmentFlag.AlignCenter)

    if has_image:
        pixmap = QPixmap(image_path)
        if not pixmap.isNull():
            scaled = pixmap.scaled(
                THUMBNAIL_CELL_W, THUMBNAIL_CELL_H,
                Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation,
            )
            label.setPixmap(scaled)
            label.setStyleSheet(f"background-color: {COLOR_CAPTION_BG};")
            return label

    label.setText("No preview")
    label.setWordWrap(True)
    label.setStyleSheet(
        f"background-color: {COLOR_CAPTION_BG}; color: {COLOR_TEXT_MUTED}; font-size: 9px;"
    )
    return label


class CameraEditDialog(QDialog):
    """Small modal popup for add/edit of a single camera's name/url/
    type. Kept as a focused modal even though Settings itself is now a
    full page -- a short add/edit form doesn't need its own section or
    permanent screen space, and modal-for-a-quick-form is still the
    right interaction even inside a page-based settings shell."""

    TYPES = ["rtsp", "tcp", "udp", "http"]

    def __init__(self, app, camera, parent=None):
        super().__init__(parent)
        self.app = app
        self.camera = camera  # None means "adding new"

        self.setWindowTitle("Add Camera" if camera is None else "Edit Camera")
        self.setMinimumWidth(420)
        self.setStyleSheet(f"background-color: {COLOR_PANEL_BG}; color: {COLOR_TEXT_PRIMARY};")

        form = QFormLayout()
        form.setSpacing(10)

        self.name_edit = QLineEdit(camera["name"] if camera else "")
        self.url_edit = QLineEdit(camera["url"] if camera else "")
        self.type_combo = QComboBox()
        self.type_combo.addItems(self.TYPES)
        if camera:
            idx = self.type_combo.findText(camera["type"])
            if idx >= 0:
                self.type_combo.setCurrentIndex(idx)

        for w in (self.name_edit, self.url_edit, self.type_combo):
            w.setStyleSheet(input_style())

        form.addRow("Name:", self.name_edit)
        form.addRow("Stream URL:", self.url_edit)
        form.addRow("Type:", self.type_combo)

        hint = QLabel(
            "Examples:\n"
            "  rtsp://user:pass@192.168.1.10:554/stream1\n"
            "  tcp://192.168.1.10:9000\n"
            "  udp://239.0.0.1:1234\n"
            "  http://192.168.1.10:8080/video"
        )
        hint.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font-size: 11px;")

        btn_row = QHBoxLayout()
        cancel_btn = QPushButton("Cancel")
        save_btn = QPushButton("Save")
        for b in (cancel_btn, save_btn):
            b.setStyleSheet(button_style())
        cancel_btn.clicked.connect(self.reject)
        save_btn.clicked.connect(self._on_save)
        btn_row.addStretch(1)
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(save_btn)

        outer = QVBoxLayout(self)
        outer.addLayout(form)
        outer.addWidget(hint)
        outer.addSpacing(8)
        outer.addLayout(btn_row)

    def _on_save(self):
        name = self.name_edit.text().strip()
        url = self.url_edit.text().strip()
        cam_type = self.type_combo.currentText()

        if not name:
            QMessageBox.critical(self, "Invalid camera", "Name cannot be empty.")
            return
        if not url:
            QMessageBox.critical(self, "Invalid camera", "Stream URL cannot be empty.")
            return

        try:
            if self.camera is None:
                self.app.add_camera(name, url, cam_type)
            else:
                self.app.update_camera(self.camera["id"], name=name, url=url, cam_type=cam_type)
        except ValueError as exc:
            QMessageBox.critical(self, "Invalid camera", str(exc))
            return

        self.accept()


class CamerasSectionPanel(QWidget):
    """Cameras section: same add/edit/remove/view CRUD the old
    SettingsDialog had, now living as a section page instead of a
    standalone modal."""

    def __init__(self, app, parent=None):
        super().__init__(parent)
        self.app = app

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 20, 24, 20)
        outer.setSpacing(12)

        heading = QLabel("Cameras")
        heading.setStyleSheet(f"color: {COLOR_TEXT_PRIMARY}; font-size: 16px; font-weight: bold;")
        outer.addWidget(heading)

        subheading = QLabel("Add, edit, or remove camera streams.")
        subheading.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font-size: 12px;")
        outer.addWidget(subheading)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Name", "Type", "URL"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.setStyleSheet(table_style())
        outer.addWidget(self.table, stretch=1)

        btn_row = QHBoxLayout()
        add_btn = QPushButton("Add…")
        edit_btn = QPushButton("Edit…")
        remove_btn = QPushButton("Remove")
        view_btn = QPushButton("View")
        for b in (add_btn, edit_btn, remove_btn, view_btn):
            b.setStyleSheet(button_style())

        add_btn.clicked.connect(self._on_add)
        edit_btn.clicked.connect(self._on_edit)
        remove_btn.clicked.connect(self._on_remove)
        view_btn.clicked.connect(self._on_view)

        btn_row.addWidget(add_btn)
        btn_row.addWidget(edit_btn)
        btn_row.addWidget(remove_btn)
        btn_row.addWidget(view_btn)
        btn_row.addStretch(1)
        outer.addLayout(btn_row)

        self.refresh()

    def refresh(self):
        """Public so SettingsPanel can re-pull data when this section
        becomes visible again (e.g. after the Zones section changed
        something that doesn't affect this table, no-op is fine too)."""
        cameras = self.app.store.list_cameras()
        self.table.setRowCount(len(cameras))
        for row, cam in enumerate(cameras):
            self.table.setItem(row, 0, QTableWidgetItem(cam["name"]))
            self.table.setItem(row, 1, QTableWidgetItem(cam["type"]))
            self.table.setItem(row, 2, QTableWidgetItem(cam["url"]))
            self.table.item(row, 0).setData(Qt.ItemDataRole.UserRole, cam["id"])

    def _selected_camera(self):
        row = self.table.currentRow()
        if row < 0:
            return None
        cam_id = self.table.item(row, 0).data(Qt.ItemDataRole.UserRole)
        return self.app.store.get_camera(cam_id)

    def _on_add(self):
        dlg = CameraEditDialog(self.app, None, parent=self)
        if dlg.exec():
            self.refresh()

    def _on_edit(self):
        cam = self._selected_camera()
        if cam is None:
            QMessageBox.information(self, "Edit camera", "Select a camera first.")
            return
        dlg = CameraEditDialog(self.app, cam, parent=self)
        if dlg.exec():
            self.refresh()

    def _on_remove(self):
        cam = self._selected_camera()
        if cam is None:
            QMessageBox.information(self, "Remove camera", "Select a camera first.")
            return
        reply = QMessageBox.question(
            self,
            "Remove camera",
            f"Remove '{cam['name']}'?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self.app.remove_camera(cam["id"])
            self.refresh()

    def _on_view(self):
        cam = self._selected_camera()
        if cam is None:
            QMessageBox.information(self, "View camera", "Select a camera first.")
            return
        self.app.show_single_view(cam)


class ZonesSectionPanel(QWidget):
    """Zones section: pick a camera, see a static snapshot of its feed
    with all zones overlaid, click a zone (in the list or directly on
    the frame) to highlight it, and edit points in place -- drag to
    reshape, Ctrl+click an edge to add a point, right-click a point to
    delete it. Whole-zone rename/remove still live in the list.

    Reuses ZoneEditorView (the same widget SingleView's "Define Zones"
    uses) rather than building a second polygon renderer -- this panel
    just drives it in a different mode: allow_new_zones=False, since
    drawing a brand new zone from scratch still belongs in the live
    camera view, not here. Editing what already exists is exactly what
    ZoneEditorView's existing drag/insert/delete-point machinery is
    for, so reusing it is what keeps this section in sync with
    SingleView for free -- one zone-rendering implementation, two
    entry points into it.

    Phase 3.5 (motion detection tuning) additions:
    - A master "Enable motion detection" checkbox + sensitivity preset
      dropdown, per camera, sitting above the zone table. Unchecking
      the master switch disables detection for every zone on this
      camera at once (camera_store.set_motion_enabled), and the
      per-zone checkboxes in the table dim + become non-interactive
      to reflect that -- they're not cleared/forgotten, just inert
      while the master switch is off.
    - A third "Detect" checkbox column in the zone table, one per zone,
      bound to that zone's detection_enabled flag. Zones with
      detection on render in a distinct teal on the canvas (see
      zone_editor.py's COLOR_ZONE_DETECTION_STROKE) so it's obvious at
      a glance which zones actually feed motion detection.
    """

    SENSITIVITY_LABELS = [("Low", "low"), ("Medium", "medium"), ("High", "high")]

    def __init__(self, app, parent=None):
        super().__init__(parent)
        self.app = app
        self.current_camera_id = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 20, 24, 20)
        outer.setSpacing(12)

        heading = QLabel("Zones")
        heading.setStyleSheet(f"color: {COLOR_TEXT_PRIMARY}; font-size: 16px; font-weight: bold;")
        outer.addWidget(heading)

        subheading = QLabel(
            "Click a zone (in the list or on the frame) to highlight it. "
            "Drag points to reshape, Ctrl+click an edge to add a point, "
            "right-click a point to delete it. To draw a brand new zone, "
            "open a camera and use \u201cDefine Zones\u201d."
        )
        subheading.setWordWrap(True)
        subheading.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font-size: 12px;")
        outer.addWidget(subheading)

        picker_row = QHBoxLayout()
        picker_label = QLabel("Camera:")
        picker_label.setStyleSheet(f"color: {COLOR_TEXT_PRIMARY};")
        picker_row.addWidget(picker_label)

        self.camera_picker = QComboBox()
        self.camera_picker.setStyleSheet(input_style())
        self.camera_picker.currentIndexChanged.connect(self._on_camera_picked)
        picker_row.addWidget(self.camera_picker, stretch=1)
        outer.addLayout(picker_row)

        # ---- motion detection controls: master switch + sensitivity --
        motion_row = QHBoxLayout()
        self.motion_enabled_check = QCheckBox("Enable motion detection for this camera")
        self.motion_enabled_check.setStyleSheet(f"color: {COLOR_TEXT_PRIMARY};")
        self.motion_enabled_check.toggled.connect(self._on_motion_enabled_toggled)
        motion_row.addWidget(self.motion_enabled_check)

        motion_row.addSpacing(16)

        sensitivity_label = QLabel("Sensitivity:")
        sensitivity_label.setStyleSheet(f"color: {COLOR_TEXT_PRIMARY};")
        motion_row.addWidget(sensitivity_label)

        self.sensitivity_combo = QComboBox()
        self.sensitivity_combo.setStyleSheet(input_style())
        for display_label, _value in self.SENSITIVITY_LABELS:
            self.sensitivity_combo.addItem(display_label)
        self.sensitivity_combo.currentIndexChanged.connect(self._on_sensitivity_changed)
        motion_row.addWidget(self.sensitivity_combo)

        motion_row.addStretch(1)
        outer.addLayout(motion_row)

        motion_hint = QLabel(
            "Turning this off disables detection for every zone on this camera. "
            "Enable detection on individual zones below to restrict motion "
            "detection to just those areas -- once any zone has detection on, "
            "whole-camera motion is ignored in favor of that zone."
        )
        motion_hint.setWordWrap(True)
        motion_hint.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font-size: 11px;")
        outer.addWidget(motion_hint)

        # ---- two-column body: zone list+actions on the left, the
        # static frame with overlaid zones on the right -------------
        body_row = QHBoxLayout()
        body_row.setSpacing(16)

        left_col = QVBoxLayout()
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Zone name", "Points", "Detect"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.setStyleSheet(table_style())
        self.table.setMaximumWidth(320)
        self.table.itemSelectionChanged.connect(self._on_table_selection_changed)
        left_col.addWidget(self.table, stretch=1)

        self.empty_hint = QLabel("This camera has no zones yet.")
        self.empty_hint.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font-size: 12px;")
        self.empty_hint.setVisible(False)
        left_col.addWidget(self.empty_hint)

        btn_row = QHBoxLayout()
        rename_btn = QPushButton("Rename…")
        remove_btn = QPushButton("Remove")
        for b in (rename_btn, remove_btn):
            b.setStyleSheet(button_style())
        rename_btn.clicked.connect(self._on_rename)
        remove_btn.clicked.connect(self._on_remove)
        btn_row.addWidget(rename_btn)
        btn_row.addWidget(remove_btn)
        btn_row.addStretch(1)
        left_col.addLayout(btn_row)

        left_container = QWidget()
        left_container.setLayout(left_col)
        left_container.setMaximumWidth(320)
        body_row.addWidget(left_container)

        # The frame viewer: static snapshot + zone overlays, point
        # editing enabled, new-zone drawing disabled.
        self.frame_viewer = ZoneEditorView()
        self.frame_viewer.set_edit_mode(True, allow_new_zones=False)
        self.frame_viewer.zoneSelected.connect(self._on_canvas_zone_selected)
        self.frame_viewer.zoneModified.connect(self._on_zone_modified_on_canvas)
        self.frame_viewer.zoneDeleteRequested.connect(self._on_zone_delete_requested_on_canvas)

        viewer_frame = QFrame()
        viewer_frame.setStyleSheet(f"background-color: {COLOR_CAPTION_BG}; border: 1px solid {COLOR_BORDER};")
        viewer_layout = QVBoxLayout(viewer_frame)
        viewer_layout.setContentsMargins(4, 4, 4, 4)
        viewer_layout.addWidget(self.frame_viewer)
        body_row.addWidget(viewer_frame, stretch=1)

        outer.addLayout(body_row, stretch=1)

        self.no_frame_hint = QLabel(
            "No frame available yet for this camera -- it may still be connecting."
        )
        self.no_frame_hint.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font-size: 11px;")
        self.no_frame_hint.setVisible(False)
        outer.addWidget(self.no_frame_hint)

        self.refresh()

    def refresh(self):
        """Public so SettingsPanel can call this when the section
        becomes visible, in case cameras were added/removed elsewhere
        since this panel was built."""
        cameras = self.app.store.list_cameras()

        self.camera_picker.blockSignals(True)
        self.camera_picker.clear()
        for cam in cameras:
            self.camera_picker.addItem(cam["name"], userData=cam["id"])
        self.camera_picker.blockSignals(False)

        if not cameras:
            self.current_camera_id = None
            self._load_camera(None)
            return

        # Try to keep the previously-selected camera selected across a
        # refresh; fall back to the first camera if it's gone.
        idx = 0
        if self.current_camera_id is not None:
            for i, cam in enumerate(cameras):
                if cam["id"] == self.current_camera_id:
                    idx = i
                    break
        self.camera_picker.setCurrentIndex(idx)
        self._load_camera(self.camera_picker.itemData(idx))

    def _on_camera_picked(self, index):
        cam_id = self.camera_picker.itemData(index) if index >= 0 else None
        self._load_camera(cam_id)

    def sync_external_change(self):
        """Called by MainWindow.notify_zones_changed when this
        camera's zones changed -- either from somewhere else
        (typically SingleView's live zone editor) or from this same
        panel's own canvas (drag/insert/delete-point all round-trip
        through notify_zones_changed too). Refreshes the table either
        way, but only rebuilds the canvas's polygon items if they're
        actually out of sync with the store -- otherwise this would
        rebuild the scene (losing in-progress interaction state) after
        every single drag-release, even though the canvas that just
        produced the edit already shows it correctly."""
        if self.current_camera_id is None:
            return
        self._refresh_zone_table()

        camera = self.app.store.get_camera(self.current_camera_id)
        if camera is None:
            return
        if self._canvas_matches_store(camera):
            return  # change originated here -- canvas is already current

        previously_selected = self.frame_viewer.selected_zone_id
        self.frame_viewer.set_camera(camera)
        if previously_selected is not None:
            self.frame_viewer.select_zone(previously_selected)

    def _canvas_matches_store(self, camera):
        stored_zones = camera.get("zones", [])
        canvas_snapshot = self.frame_viewer.get_zone_snapshot()  # {id: (name, [(x,y),...], detection_enabled)}
        if len(stored_zones) != len(canvas_snapshot):
            return False
        for zone in stored_zones:
            entry = canvas_snapshot.get(zone["id"])
            if entry is None:
                return False
            canvas_name, canvas_points, canvas_detection_enabled = entry
            if canvas_name != zone.get("name", ""):
                return False
            if canvas_detection_enabled != zone.get("detection_enabled", False):
                return False
            if len(canvas_points) != len(zone["points"]):
                return False
            for (sx, sy), (cx, cy) in zip(zone["points"], canvas_points):
                # Small tolerance for floating point round-trip through
                # scene coordinates and back.
                if abs(cx - sx) > 1e-4 or abs(cy - sy) > 1e-4:
                    return False
        return True

    def _load_camera(self, cam_id):
        """Switch the panel to a different camera: reload the zone
        list, grab one static frame from the live stream (if any), and
        push both into the embedded ZoneEditorView. Also loads this
        camera's motion_enabled/motion_sensitivity into the controls
        above the table -- signals blocked while doing so, since
        populating the controls to match stored state is not itself a
        user edit and shouldn't trigger a save."""
        self.current_camera_id = cam_id

        if cam_id is None:
            self.table.setRowCount(0)
            self.empty_hint.setVisible(True)
            self.frame_viewer.set_camera(None)
            self.no_frame_hint.setVisible(False)
            self.motion_enabled_check.setEnabled(False)
            self.sensitivity_combo.setEnabled(False)
            return

        camera = self.app.store.get_camera(cam_id)

        motion_enabled = self.app.store.get_motion_enabled(cam_id)
        sensitivity = self.app.store.get_motion_sensitivity(cam_id)
        self.motion_enabled_check.setEnabled(True)
        self.motion_enabled_check.blockSignals(True)
        self.motion_enabled_check.setChecked(motion_enabled)
        self.motion_enabled_check.blockSignals(False)

        self.sensitivity_combo.setEnabled(True)
        self.sensitivity_combo.blockSignals(True)
        sensitivity_values = [value for _label, value in self.SENSITIVITY_LABELS]
        try:
            idx = sensitivity_values.index(sensitivity)
        except ValueError:
            idx = sensitivity_values.index("medium")
        self.sensitivity_combo.setCurrentIndex(idx)
        self.sensitivity_combo.blockSignals(False)

        self._refresh_zone_table()
        self._apply_master_switch_visual_state(motion_enabled)
        self.frame_viewer.set_camera(camera)

        # One static snapshot -- not a live feed. get_frame() returns
        # whatever the background StreamWorker most recently decoded;
        # if the camera hasn't connected yet this is None, which
        # update_frame() already no-ops on safely.
        frame = self.app.streams.get_frame(cam_id)
        if frame is None:
            self.no_frame_hint.setVisible(True)
        else:
            self.no_frame_hint.setVisible(False)
            self.frame_viewer.update_frame(frame)

    def _refresh_zone_table(self):
        if self.current_camera_id is None:
            self.table.setRowCount(0)
            self.empty_hint.setVisible(True)
            return

        zones = self.app.store.get_zones(self.current_camera_id)
        master_enabled = self.app.store.get_motion_enabled(self.current_camera_id)

        self.table.blockSignals(True)
        self.table.setRowCount(len(zones))
        for row, zone in enumerate(zones):
            name_item = QTableWidgetItem(zone["name"])
            name_item.setData(Qt.ItemDataRole.UserRole, zone["id"])
            self.table.setItem(row, 0, name_item)
            self.table.setItem(row, 1, QTableWidgetItem(str(len(zone["points"]))))

            # Detect checkbox -- a real widget in the cell, not a
            # QTableWidgetItem, since QTableWidgetItem has no built-in
            # checkbox-with-callback; setCellWidget gives us a normal
            # QCheckBox we can wire a signal to directly.
            checkbox = QCheckBox()
            checkbox.setChecked(zone.get("detection_enabled", False))
            checkbox.setEnabled(master_enabled)
            # Capture zone_id by default arg, not closure-over-loop-var,
            # to avoid the classic late-binding bug where every
            # checkbox's callback would otherwise see the LAST row's
            # zone_id.
            checkbox.toggled.connect(
                lambda checked, zone_id=zone["id"]: self._on_zone_detection_toggled(zone_id, checked)
            )
            self._style_detect_checkbox(checkbox, master_enabled)

            cell_container = QWidget()
            cell_layout = QHBoxLayout(cell_container)
            cell_layout.setContentsMargins(0, 0, 0, 0)
            cell_layout.addStretch(1)
            cell_layout.addWidget(checkbox)
            cell_layout.addStretch(1)
            self.table.setCellWidget(row, 2, cell_container)
        self.table.blockSignals(False)

        self.empty_hint.setVisible(len(zones) == 0)

    def _style_detect_checkbox(self, checkbox, master_enabled):
        """Visually dim a zone's Detect checkbox when the camera-level
        master switch is off, per the design: the per-zone checkboxes
        stay visible (the column isn't hidden) but read as inert."""
        color = COLOR_TEXT_PRIMARY if master_enabled else COLOR_TEXT_MUTED
        checkbox.setStyleSheet(f"QCheckBox {{ color: {color}; }}")

    def _apply_master_switch_visual_state(self, master_enabled):
        """Re-applies the dim/enabled visual state to every existing
        Detect checkbox in the table without rebuilding the whole
        table -- called right after the master switch is toggled, and
        once when a camera is first loaded."""
        for row in range(self.table.rowCount()):
            cell_container = self.table.cellWidget(row, 2)
            if cell_container is None:
                continue
            checkbox = cell_container.findChild(QCheckBox)
            if checkbox is None:
                continue
            checkbox.setEnabled(master_enabled)
            self._style_detect_checkbox(checkbox, master_enabled)

    def _selected_zone_id(self):
        row = self.table.currentRow()
        if row < 0:
            return None
        return self.table.item(row, 0).data(Qt.ItemDataRole.UserRole)

    # ----- selection sync: list <-> canvas -------------------------------

    def _on_table_selection_changed(self):
        zone_id = self._selected_zone_id()
        # Avoid feedback loops: only push to the canvas if it doesn't
        # already agree (canvas-originated selection already set this
        # same row via _on_canvas_zone_selected below).
        if self.frame_viewer.selected_zone_id != zone_id:
            self.frame_viewer.select_zone(zone_id)

    def _on_canvas_zone_selected(self, zone_id):
        if zone_id is None:
            self.table.clearSelection()
            return
        for row in range(self.table.rowCount()):
            if self.table.item(row, 0).data(Qt.ItemDataRole.UserRole) == zone_id:
                if self.table.currentRow() != row:
                    self.table.setCurrentCell(row, 0)
                break

    # ----- point-level editing callbacks from the canvas ----------------

    def _on_zone_modified_on_canvas(self, zone_id, normalized_points):
        if self.current_camera_id is None:
            return
        try:
            self.app.store.update_zone(self.current_camera_id, zone_id, points=normalized_points)
        except ValueError as exc:
            QMessageBox.critical(self, "Couldn't update zone", str(exc))
            return
        self.app.notify_zones_changed(self.current_camera_id)
        self._refresh_zone_table()

    def _on_zone_delete_requested_on_canvas(self, zone_id):
        # The canvas's own delete glyph is hidden when allow_new_zones
        # is False (see ZoneEditorView.set_edit_mode), so this signal
        # shouldn't normally fire here -- but route it through the same
        # confirm-and-remove path as the table's Remove button just in
        # case, rather than silently ignoring it.
        self._remove_zone(zone_id)

    # ----- rename / remove (whole-zone actions) --------------------------

    def _on_rename(self):
        if self.current_camera_id is None:
            return
        zone_id = self._selected_zone_id()
        if zone_id is None:
            QMessageBox.information(self, "Rename zone", "Select a zone first.")
            return

        zone = self.app.store.get_zone(self.current_camera_id, zone_id)
        if zone is None:
            return

        new_name, ok = QInputDialog.getText(self, "Rename zone", "Zone name:", text=zone["name"])
        new_name = (new_name or "").strip()
        if not ok or not new_name:
            return

        try:
            self.app.store.update_zone(self.current_camera_id, zone_id, name=new_name)
        except ValueError as exc:
            QMessageBox.critical(self, "Couldn't rename zone", str(exc))
            return

        self.frame_viewer.refresh_zone(zone_id, name=new_name)
        self._refresh_zone_table()
        self.app.notify_zones_changed(self.current_camera_id)

    def _on_remove(self):
        zone_id = self._selected_zone_id()
        if zone_id is None:
            QMessageBox.information(self, "Remove zone", "Select a zone first.")
            return
        self._remove_zone(zone_id)

    def _remove_zone(self, zone_id):
        if self.current_camera_id is None:
            return
        zone = self.app.store.get_zone(self.current_camera_id, zone_id)
        zone_name = zone["name"] if zone else "this zone"
        reply = QMessageBox.question(
            self,
            "Remove zone",
            f"Remove '{zone_name}'?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        self.app.store.remove_zone(self.current_camera_id, zone_id)
        self.frame_viewer.remove_zone_from_scene(zone_id)
        self.app.notify_zones_changed(self.current_camera_id)
        self._refresh_zone_table()

    # ----- motion detection tuning (Phase 3.5) --------------------------

    def _on_motion_enabled_toggled(self, checked):
        if self.current_camera_id is None:
            return
        self.app.store.set_motion_enabled(self.current_camera_id, checked)
        # Per-zone checkboxes dim/un-dim to match, without touching
        # their actual detection_enabled values -- turning the master
        # switch back on should restore whichever zones were
        # individually enabled before, not reset them.
        self._apply_master_switch_visual_state(checked)
        # The backend (MotionWorker) reads motion_enabled fresh from
        # camera_store on every processed frame, so no separate
        # "restart detection" call is needed here -- the next poll
        # tick already reflects the new switch state. We still go
        # through notify_zones_changed's broader sync path for
        # consistency with every other zone/detection mutation in this
        # file, even though its zone-table-refresh and canvas-sync
        # parts are no-ops for this particular change.
        self.app.notify_zones_changed(self.current_camera_id)

    def _on_sensitivity_changed(self, index):
        if self.current_camera_id is None or index < 0:
            return
        _label, value = self.SENSITIVITY_LABELS[index]
        self.app.store.set_motion_sensitivity(self.current_camera_id, value)

    def _on_zone_detection_toggled(self, zone_id, checked):
        if self.current_camera_id is None:
            return
        try:
            self.app.store.update_zone(self.current_camera_id, zone_id, detection_enabled=checked)
        except ValueError as exc:
            QMessageBox.critical(self, "Couldn't update zone", str(exc))
            return

        # Update the canvas color in place (no full rebuild -- same
        # pattern as a rename's refresh_zone call) and route through
        # the usual notify hub so the live SingleView canvas (if open
        # on this same camera) and the motion mask cache both pick up
        # the change immediately.
        self.frame_viewer.set_zone_detection_enabled(zone_id, checked)
        self.app.notify_zones_changed(self.current_camera_id)


class ObjectDetectionSectionPanel(QWidget):
    """Phase 4 settings section: per-camera object detection tuning --
    master on/off, trigger mode (on-motion vs continuous), and which
    COCO classes to keep. Deliberately simpler than ZonesSectionPanel
    -- there's no canvas here, since there's nothing spatial to edit;
    cropping happens automatically off each zone's existing geometry
    in object_detector.py. Unlike zone/motion changes, nothing here
    needs to route through app.notify_zones_changed -- the detection
    worker re-reads camera settings straight from the store on every
    tick rather than caching them, so a plain store write is enough
    for the change to take effect on the next tick.
    """

    CANDIDATE_CLASSES = [
        "person", "car", "truck", "bus", "motorcycle",
        "bicycle", "dog", "cat",
    ]

    MODE_LABELS = [("On motion", "on_motion"), ("Continuous", "continuous")]

    def __init__(self, app, parent=None):
        super().__init__(parent)
        self.app = app
        self.current_camera_id = None
        self._class_checkboxes = {}  # class_name -> QCheckBox
        self._class_confidence_spinboxes = {}  # class_name -> QSpinBox (percent)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 20, 24, 20)
        outer.setSpacing(12)

        heading = QLabel("Object Detection")
        heading.setStyleSheet(f"color: {COLOR_TEXT_PRIMARY}; font-size: 16px; font-weight: bold;")
        outer.addWidget(heading)

        subheading = QLabel(
            "Classifies what triggered motion using YOLOv8n. By default "
            "it only runs when a zone (or the whole frame, for cameras "
            "with no zones opted into detection) flags motion -- cropped "
            "to that zone plus some padding for context, with a 1s "
            "cooldown between repeat checks of the same area. Switch to "
            "Continuous to run on a fixed interval regardless of motion. "
            "Each class below tracks multiple simultaneous instances "
            "independently -- 2 people and a car all showing up at once "
            "are reported separately, not collapsed into one detection."
        )
        subheading.setWordWrap(True)
        subheading.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font-size: 12px;")
        outer.addWidget(subheading)

        picker_row = QHBoxLayout()
        picker_label = QLabel("Camera:")
        picker_label.setStyleSheet(f"color: {COLOR_TEXT_PRIMARY};")
        picker_row.addWidget(picker_label)
        self.camera_picker = QComboBox()
        self.camera_picker.setStyleSheet(input_style())
        self.camera_picker.currentIndexChanged.connect(self._on_camera_picked)
        picker_row.addWidget(self.camera_picker, stretch=1)
        outer.addLayout(picker_row)

        self.enabled_check = QCheckBox("Enable object detection for this camera")
        self.enabled_check.setStyleSheet(f"color: {COLOR_TEXT_PRIMARY};")
        self.enabled_check.toggled.connect(self._on_enabled_toggled)
        outer.addWidget(self.enabled_check)

        mode_row = QHBoxLayout()
        mode_label = QLabel("Trigger mode:")
        mode_label.setStyleSheet(f"color: {COLOR_TEXT_PRIMARY};")
        mode_row.addWidget(mode_label)
        self.mode_combo = QComboBox()
        self.mode_combo.setStyleSheet(input_style())
        for display_label, _value in self.MODE_LABELS:
            self.mode_combo.addItem(display_label)
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        mode_row.addWidget(self.mode_combo)
        mode_row.addStretch(1)
        outer.addLayout(mode_row)

        classes_label = QLabel("Classes to detect:")
        classes_label.setStyleSheet(f"color: {COLOR_TEXT_PRIMARY}; margin-top: 6px;")
        outer.addWidget(classes_label)

        classes_hint = QLabel(
            "Only checked classes are logged. The percentage is this "
            "camera's own minimum confidence for that class -- e.g. "
            "\"Person\" at 70% on a busy street camera to cut down false "
            "positives, but \"Car\" at 40% on the same camera since cars "
            "are rarely misclassified. Only matters while the class is "
            "checked."
        )
        classes_hint.setWordWrap(True)
        classes_hint.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font-size: 11px;")
        outer.addWidget(classes_hint)

        classes_grid = QHBoxLayout()
        col_a = QVBoxLayout()
        col_b = QVBoxLayout()
        for i, class_name in enumerate(self.CANDIDATE_CLASSES):
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)

            checkbox = QCheckBox(class_name.capitalize())
            checkbox.setStyleSheet(f"color: {COLOR_TEXT_PRIMARY};")
            checkbox.toggled.connect(self._on_class_toggled)
            self._class_checkboxes[class_name] = checkbox
            row.addWidget(checkbox)

            conf_spin = QSpinBox()
            conf_spin.setRange(1, 99)
            conf_spin.setSuffix("%")
            conf_spin.setStyleSheet(input_style())
            conf_spin.setFixedWidth(70)
            conf_spin.setEnabled(False)  # only meaningful once this class is checked
            conf_spin.valueChanged.connect(
                lambda value, cls=class_name: self._on_class_confidence_changed(cls, value)
            )
            self._class_confidence_spinboxes[class_name] = conf_spin
            row.addWidget(conf_spin)
            row.addStretch(1)

            row_container = QWidget()
            row_container.setLayout(row)
            (col_a if i % 2 == 0 else col_b).addWidget(row_container)
        classes_grid.addLayout(col_a)
        classes_grid.addLayout(col_b)
        classes_grid.addStretch(1)
        outer.addLayout(classes_grid)

        # Quality-of-life: without this, enabling detection with every
        # class unchecked is a silent no-op -- the worker skips
        # inference entirely (see object_detector.py's allowed_classes
        # check) with no feedback anywhere that anything's wrong.
        self.no_classes_warning = QLabel(
            "⚠ No classes selected -- detection is enabled but nothing will be logged."
        )
        self.no_classes_warning.setWordWrap(True)
        self.no_classes_warning.setStyleSheet(f"color: {COLOR_STATUS_CONNECTING}; font-size: 11px;")
        self.no_classes_warning.setVisible(False)
        outer.addWidget(self.no_classes_warning)

        outer.addStretch(1)

        self.refresh()

    def refresh(self):
        """Public so SettingsPanel can re-pull data when this section
        becomes visible again (e.g. after a camera was added/removed
        elsewhere)."""
        cameras = self.app.store.list_cameras()
        self.camera_picker.blockSignals(True)
        self.camera_picker.clear()
        for cam in cameras:
            self.camera_picker.addItem(cam["name"], userData=cam["id"])
        self.camera_picker.blockSignals(False)

        if not cameras:
            self.current_camera_id = None
            self._load_camera(None)
            return

        idx = 0
        if self.current_camera_id is not None:
            for i, cam in enumerate(cameras):
                if cam["id"] == self.current_camera_id:
                    idx = i
                    break
        self.camera_picker.setCurrentIndex(idx)
        self._load_camera(self.camera_picker.itemData(idx))

    def _on_camera_picked(self, index):
        cam_id = self.camera_picker.itemData(index) if index >= 0 else None
        self._load_camera(cam_id)

    def _load_camera(self, cam_id):
        """Switch the panel to a different camera -- signals blocked
        while populating controls to match stored state, since that's
        not itself a user edit and shouldn't trigger a save (same
        pattern as ZonesSectionPanel._load_camera)."""
        self.current_camera_id = cam_id
        controls_enabled = cam_id is not None
        self.enabled_check.setEnabled(controls_enabled)
        self.mode_combo.setEnabled(controls_enabled)
        for cb in self._class_checkboxes.values():
            cb.setEnabled(controls_enabled)

        if cam_id is None:
            self.enabled_check.blockSignals(True)
            self.enabled_check.setChecked(False)
            self.enabled_check.blockSignals(False)
            for name, cb in self._class_checkboxes.items():
                cb.blockSignals(True)
                cb.setChecked(False)
                cb.blockSignals(False)
                spin = self._class_confidence_spinboxes[name]
                spin.setEnabled(False)
            self._update_no_classes_warning()
            return

        enabled = self.app.store.get_object_detection_enabled(cam_id)
        mode = self.app.store.get_object_detection_mode(cam_id)
        classes = set(self.app.store.get_object_detection_classes(cam_id))

        self.enabled_check.blockSignals(True)
        self.enabled_check.setChecked(enabled)
        self.enabled_check.blockSignals(False)

        self.mode_combo.blockSignals(True)
        mode_values = [value for _label, value in self.MODE_LABELS]
        try:
            idx = mode_values.index(mode)
        except ValueError:
            idx = mode_values.index("on_motion")
        self.mode_combo.setCurrentIndex(idx)
        self.mode_combo.blockSignals(False)

        for class_name, cb in self._class_checkboxes.items():
            is_checked = class_name in classes
            cb.blockSignals(True)
            cb.setChecked(is_checked)
            cb.blockSignals(False)

            spin = self._class_confidence_spinboxes[class_name]
            threshold = self.app.store.get_class_confidence(cam_id, class_name)
            spin.blockSignals(True)
            spin.setValue(round(threshold * 100))
            spin.blockSignals(False)
            spin.setEnabled(is_checked)

        self._update_no_classes_warning()

    def _update_no_classes_warning(self):
        """Shows the warning only when detection is enabled for the
        current camera AND every class checkbox is unchecked -- that
        combination is a silent no-op in object_detector.py otherwise."""
        if self.current_camera_id is None:
            self.no_classes_warning.setVisible(False)
            return
        enabled = self.enabled_check.isChecked()
        any_checked = any(cb.isChecked() for cb in self._class_checkboxes.values())
        self.no_classes_warning.setVisible(enabled and not any_checked)

    def _on_enabled_toggled(self, checked):
        if self.current_camera_id is None:
            return
        self.app.store.set_object_detection_enabled(self.current_camera_id, checked)
        self._update_no_classes_warning()

    def _on_mode_changed(self, index):
        if self.current_camera_id is None or index < 0:
            return
        _label, value = self.MODE_LABELS[index]
        self.app.store.set_object_detection_mode(self.current_camera_id, value)

    def _on_class_toggled(self, _checked):
        if self.current_camera_id is None:
            return
        selected = [name for name, cb in self._class_checkboxes.items() if cb.isChecked()]
        self.app.store.set_object_detection_classes(self.current_camera_id, selected)
        for name, spin in self._class_confidence_spinboxes.items():
            spin.setEnabled(self._class_checkboxes[name].isChecked())
        self._update_no_classes_warning()

    def _on_class_confidence_changed(self, class_name, percent_value):
        if self.current_camera_id is None:
            return
        self.app.store.set_class_confidence_threshold(
            self.current_camera_id, class_name, percent_value / 100.0
        )


class AlertRuleEditDialog(QDialog):
    """Modal add/edit form for a single alert rule -- same "focused
    modal for a quick form" pattern as CameraEditDialog. Handles both
    trigger types in one form (rather than two separate dialogs) since
    switching trigger_type on an existing rule is a normal edit, not a
    different kind of object."""

    TRIGGER_LABELS = [("Motion (any)", "motion"), ("Object class detected", "object_class")]

    def __init__(self, app, cam_id, rule, parent=None):
        super().__init__(parent)
        self.app = app
        self.cam_id = cam_id
        self.rule = rule  # None means "adding new"

        self.setWindowTitle("Add Alert Rule" if rule is None else "Edit Alert Rule")
        self.setMinimumWidth(420)
        self.setStyleSheet(f"background-color: {COLOR_PANEL_BG}; color: {COLOR_TEXT_PRIMARY};")

        form = QFormLayout()
        form.setSpacing(10)

        self.name_edit = QLineEdit(rule["name"] if rule else "")
        self.name_edit.setStyleSheet(input_style())
        form.addRow("Name:", self.name_edit)

        self.start_edit = QTimeEdit()
        self.start_edit.setDisplayFormat("HH:mm")
        self.start_edit.setStyleSheet(input_style())
        self.end_edit = QTimeEdit()
        self.end_edit.setDisplayFormat("HH:mm")
        self.end_edit.setStyleSheet(input_style())
        if rule:
            self.start_edit.setTime(QTime.fromString(rule["start"], "HH:mm"))
            self.end_edit.setTime(QTime.fromString(rule["end"], "HH:mm"))
        else:
            self.start_edit.setTime(QTime(22, 0))
            self.end_edit.setTime(QTime(6, 0))
        form.addRow("Start:", self.start_edit)
        form.addRow("End:", self.end_edit)

        window_hint = QLabel("End before Start means the window crosses midnight.")
        window_hint.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font-size: 11px;")
        form.addRow("", window_hint)

        # can_use_object_class_trigger() is the tier chokepoint (see
        # camera_store.py) -- always True today, but the option is
        # only offered here at all if the store says it's allowed,
        # rather than being offered-then-rejected on save.
        self._allow_object_class = self.app.store.can_use_object_class_trigger(cam_id)
        self.trigger_combo = QComboBox()
        self.trigger_combo.setStyleSheet(input_style())
        for label, value in self.TRIGGER_LABELS:
            if value == "object_class" and not self._allow_object_class:
                continue
            self.trigger_combo.addItem(label, userData=value)
        self.trigger_combo.currentIndexChanged.connect(self._on_trigger_changed)
        form.addRow("Trigger:", self.trigger_combo)

        self.enabled_check = QCheckBox("Enabled")
        self.enabled_check.setChecked(rule.get("enabled", True) if rule else True)
        self.enabled_check.setStyleSheet(f"color: {COLOR_TEXT_PRIMARY};")
        form.addRow("", self.enabled_check)

        self.classes_label = QLabel("Classes:")
        self.classes_label.setStyleSheet(f"color: {COLOR_TEXT_PRIMARY};")
        classes_row = QHBoxLayout()
        self._class_checkboxes = {}
        rule_classes = set(rule.get("classes", [])) if rule else set()
        col_a = QVBoxLayout()
        col_b = QVBoxLayout()
        for i, class_name in enumerate(ObjectDetectionSectionPanel.CANDIDATE_CLASSES):
            cb = QCheckBox(class_name.capitalize())
            cb.setChecked(class_name in rule_classes)
            cb.setStyleSheet(f"color: {COLOR_TEXT_PRIMARY};")
            cb.toggled.connect(self._update_no_classes_warning)
            self._class_checkboxes[class_name] = cb
            (col_a if i % 2 == 0 else col_b).addWidget(cb)
        classes_row.addLayout(col_a)
        classes_row.addLayout(col_b)
        classes_row.addStretch(1)
        form.addRow(self.classes_label, classes_row)

        self.no_classes_warning = QLabel(
            "⚠ No classes selected -- this rule will never match."
        )
        self.no_classes_warning.setWordWrap(True)
        self.no_classes_warning.setStyleSheet(f"color: {COLOR_STATUS_CONNECTING}; font-size: 11px;")
        form.addRow("", self.no_classes_warning)

        btn_row = QHBoxLayout()
        cancel_btn = QPushButton("Cancel")
        save_btn = QPushButton("Save")
        for b in (cancel_btn, save_btn):
            b.setStyleSheet(button_style())
        cancel_btn.clicked.connect(self.reject)
        save_btn.clicked.connect(self._on_save)
        btn_row.addStretch(1)
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(save_btn)

        outer = QVBoxLayout(self)
        outer.addLayout(form)
        outer.addSpacing(8)
        outer.addLayout(btn_row)

        # Initialize trigger selection + class-row visibility.
        if rule:
            idx = self.trigger_combo.findData(rule["trigger_type"])
            self.trigger_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self._on_trigger_changed(self.trigger_combo.currentIndex())

    def _current_trigger_type(self):
        return self.trigger_combo.currentData()

    def _on_trigger_changed(self, _index):
        is_object_class = self._current_trigger_type() == "object_class"
        self.classes_label.setVisible(is_object_class)
        for cb in self._class_checkboxes.values():
            cb.setVisible(is_object_class)
        self._update_no_classes_warning()

    def _update_no_classes_warning(self, *_args):
        is_object_class = self._current_trigger_type() == "object_class"
        any_checked = any(cb.isChecked() for cb in self._class_checkboxes.values())
        self.no_classes_warning.setVisible(is_object_class and not any_checked)

    def _on_save(self):
        name = self.name_edit.text().strip()
        if not name:
            QMessageBox.critical(self, "Invalid rule", "Name cannot be empty.")
            return

        start = self.start_edit.time().toString("HH:mm")
        end = self.end_edit.time().toString("HH:mm")
        trigger_type = self._current_trigger_type()
        classes = [name for name, cb in self._class_checkboxes.items() if cb.isChecked()]
        enabled = self.enabled_check.isChecked()

        try:
            if self.rule is None:
                self.app.store.add_alert_rule(
                    self.cam_id, name, start, end, trigger_type,
                    classes=classes, enabled=enabled,
                )
            else:
                self.app.store.update_alert_rule(
                    self.cam_id, self.rule["id"], name=name, start=start, end=end,
                    trigger_type=trigger_type, classes=classes, enabled=enabled,
                )
        except ValueError as exc:
            QMessageBox.critical(self, "Couldn't save rule", str(exc))
            return

        self.accept()


class AlertsSectionPanel(QWidget):
    """Phase 5 settings section: per-camera time-of-day alert rules.
    Same shape as ZonesSectionPanel/ObjectDetectionSectionPanel (camera
    picker + a table), but simpler than Zones -- there's no canvas,
    since rules are per-camera not per-zone (see the Phase 5 design
    discussion). Unlike zone/motion changes, nothing here needs to
    route through app.notify_zones_changed -- AlertWorker re-reads
    camera settings straight from the store on every tick, same as
    ObjectDetectionSectionPanel's reasoning."""

    def __init__(self, app, parent=None):
        super().__init__(parent)
        self.app = app
        self.current_camera_id = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 20, 24, 20)
        outer.setSpacing(12)

        heading = QLabel("Alerts")
        heading.setStyleSheet(f"color: {COLOR_TEXT_PRIMARY}; font-size: 16px; font-weight: bold;")
        outer.addWidget(heading)

        subheading = QLabel(
            "Rules fire once when they start matching and once when they stop, "
            "logging how long the match lasted -- not repeatedly while it "
            "continues. A rule can trigger on any motion, or on a specific "
            "object class being detected, within a time window (windows "
            "that end before they start cross midnight)."
        )
        subheading.setWordWrap(True)
        subheading.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font-size: 12px;")
        outer.addWidget(subheading)

        picker_row = QHBoxLayout()
        picker_label = QLabel("Camera:")
        picker_label.setStyleSheet(f"color: {COLOR_TEXT_PRIMARY};")
        picker_row.addWidget(picker_label)
        self.camera_picker = QComboBox()
        self.camera_picker.setStyleSheet(input_style())
        self.camera_picker.currentIndexChanged.connect(self._on_camera_picked)
        picker_row.addWidget(self.camera_picker, stretch=1)
        outer.addLayout(picker_row)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Name", "Window", "Trigger", "Enabled"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.setStyleSheet(table_style())
        outer.addWidget(self.table, stretch=1)

        self.empty_hint = QLabel("This camera has no alert rules yet.")
        self.empty_hint.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font-size: 12px;")
        self.empty_hint.setVisible(False)
        outer.addWidget(self.empty_hint)

        btn_row = QHBoxLayout()
        add_btn = QPushButton("Add Rule…")
        edit_btn = QPushButton("Edit Rule…")
        remove_btn = QPushButton("Remove")
        for b in (add_btn, edit_btn, remove_btn):
            b.setStyleSheet(button_style())
        add_btn.clicked.connect(self._on_add)
        edit_btn.clicked.connect(self._on_edit)
        remove_btn.clicked.connect(self._on_remove)
        btn_row.addWidget(add_btn)
        btn_row.addWidget(edit_btn)
        btn_row.addWidget(remove_btn)
        btn_row.addStretch(1)
        outer.addLayout(btn_row)

        self.refresh()

    def refresh(self):
        cameras = self.app.store.list_cameras()
        self.camera_picker.blockSignals(True)
        self.camera_picker.clear()
        for cam in cameras:
            self.camera_picker.addItem(cam["name"], userData=cam["id"])
        self.camera_picker.blockSignals(False)

        if not cameras:
            self.current_camera_id = None
            self._load_camera(None)
            return

        idx = 0
        if self.current_camera_id is not None:
            for i, cam in enumerate(cameras):
                if cam["id"] == self.current_camera_id:
                    idx = i
                    break
        self.camera_picker.setCurrentIndex(idx)
        self._load_camera(self.camera_picker.itemData(idx))

    def _on_camera_picked(self, index):
        cam_id = self.camera_picker.itemData(index) if index >= 0 else None
        self._load_camera(cam_id)

    def _load_camera(self, cam_id):
        self.current_camera_id = cam_id
        self._refresh_table()

    def _refresh_table(self):
        if self.current_camera_id is None:
            self.table.setRowCount(0)
            self.empty_hint.setVisible(True)
            return

        rules = self.app.store.get_alert_rules(self.current_camera_id)
        value_to_label = {value: label for label, value in AlertRuleEditDialog.TRIGGER_LABELS}

        self.table.setRowCount(len(rules))
        for row, rule in enumerate(rules):
            name_item = QTableWidgetItem(rule["name"])
            name_item.setData(Qt.ItemDataRole.UserRole, rule["id"])
            self.table.setItem(row, 0, name_item)
            self.table.setItem(row, 1, QTableWidgetItem(f"{rule['start']}–{rule['end']}"))

            trigger_text = value_to_label.get(rule["trigger_type"], rule["trigger_type"])
            if rule["trigger_type"] == "object_class" and rule.get("classes"):
                trigger_text += f" ({', '.join(rule['classes'])})"
            self.table.setItem(row, 2, QTableWidgetItem(trigger_text))

            enabled_item = QTableWidgetItem("Yes" if rule.get("enabled", True) else "No")
            self.table.setItem(row, 3, enabled_item)

        self.empty_hint.setVisible(len(rules) == 0)

    def _selected_rule_id(self):
        row = self.table.currentRow()
        if row < 0:
            return None
        return self.table.item(row, 0).data(Qt.ItemDataRole.UserRole)

    def _on_add(self):
        if self.current_camera_id is None:
            QMessageBox.information(self, "Add rule", "Add a camera first.")
            return
        dlg = AlertRuleEditDialog(self.app, self.current_camera_id, None, parent=self)
        if dlg.exec():
            self._refresh_table()

    def _on_edit(self):
        rule_id = self._selected_rule_id()
        if rule_id is None:
            QMessageBox.information(self, "Edit rule", "Select a rule first.")
            return
        rule = self.app.store.get_alert_rule(self.current_camera_id, rule_id)
        if rule is None:
            return
        dlg = AlertRuleEditDialog(self.app, self.current_camera_id, rule, parent=self)
        if dlg.exec():
            self._refresh_table()

    def _on_remove(self):
        rule_id = self._selected_rule_id()
        if rule_id is None:
            QMessageBox.information(self, "Remove rule", "Select a rule first.")
            return
        rule = self.app.store.get_alert_rule(self.current_camera_id, rule_id)
        rule_name = rule["name"] if rule else "this rule"
        reply = QMessageBox.question(
            self,
            "Remove rule",
            f"Remove '{rule_name}'?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self.app.store.remove_alert_rule(self.current_camera_id, rule_id)
        self._refresh_table()


def _format_size(num_bytes):
    """Human-readable file size for the Recordings table. None (a
    segment still open, or a file whose size couldn't be read) reads
    as "recording..." rather than a blank/zero, which would look like
    an empty or broken file."""
    if num_bytes is None:
        return "Recording…"
    mb = num_bytes / (1024 * 1024)
    if mb >= 1024:
        return f"{mb / 1024:.2f} GB"
    return f"{mb:.1f} MB"


def _format_iso_for_display(iso_str):
    """ISO8601 ('2026-07-25T14:00:00') -> a friendlier space-separated
    form for the table -- no timezone math here, these are all local
    wall-clock times written by datetime.now().isoformat() at the
    source (event_store.py / recording_manager.py / event_logger.py),
    same convention alert_manager.py's log lines already use."""
    if not iso_str:
        return "—"
    return iso_str.replace("T", " ")


def _format_duration_seconds(total_seconds):
    total_seconds = max(0, int(total_seconds))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


def _format_duration(start_iso, end_iso):
    """Duration column for the Recordings table. A still-open segment
    (end_iso None) computes elapsed time against now instead of a
    stored end_time, and is labelled "so far" -- there's no live timer
    ticking this (see the Recordings design discussion: auto-refresh
    was explicitly not requested), so the number is only as fresh as
    the last time this row was rendered, but it's never wrong at the
    moment it's shown."""
    try:
        start_dt = datetime.fromisoformat(start_iso)
    except (TypeError, ValueError):
        return "—"
    if end_iso:
        try:
            end_dt = datetime.fromisoformat(end_iso)
        except ValueError:
            return "—"
        return _format_duration_seconds((end_dt - start_dt).total_seconds())
    return f"{_format_duration_seconds((datetime.now() - start_dt).total_seconds())} so far"


class RecordingsSectionPanel(QWidget):
    """Phase 7 settings section: per-camera list of recorded segments
    from event_store's `segments` table. Play hands the file off to
    the OS's default video player -- no in-app player, per the Phase 7
    design discussion (recordings are plain .mp4 files on disk;
    there's no reason to reimplement what every OS already does
    well). Folder reveals the file in the OS's file manager. Delete
    permanently removes one segment (DB row + file) -- disabled for a
    segment still being recorded, since deleting a file a
    cv2.VideoWriter still has open behaves inconsistently across
    platforms (see EventStore.delete_segment's docstring). Same
    camera-picker-plus-table shape as AlertsSectionPanel.

    A segment still being recorded (end_time NULL) is listed too,
    showing "Recording..." in place of an end time and a live-read
    file size (not yet in file_size_bytes, which is only written when
    the segment closes -- see RecordingWorker._close_current_segment)
    -- its file is valid and playable-so-far the whole time
    cv2.VideoWriter has it open, so there's no reason to hide it until
    it rolls to the next segment.

    The storage summary (this camera's total + all-cameras total) is
    recomputed on every _refresh_table() call -- camera switch, manual
    Refresh, or after a Delete -- not on a timer, same "only as fresh
    as the last render" tradeoff the Duration column's "so far" values
    make (auto-refresh was explicitly not requested, see the
    Recordings QoL design discussion).

    Deliberately does NOT list events (motion_start/motion_end/object-
    detection rows) alongside segments -- that's a reasonable future
    addition (e.g. showing event markers next to the segment they fell
    in) but out of scope for what was asked here: browse, play back,
    and manage recordings by camera."""

    def __init__(self, app, parent=None):
        super().__init__(parent)
        self.app = app
        self.current_camera_id = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 20, 24, 20)
        outer.setSpacing(12)

        heading = QLabel("Recordings")
        heading.setStyleSheet(f"color: {COLOR_TEXT_PRIMARY}; font-size: 16px; font-weight: bold;")
        outer.addWidget(heading)

        subheading = QLabel(
            "Every camera records continuously in 30-minute segments, kept "
            "for 14 days. Click Play to open a clip in your system's "
            "default video player."
        )
        subheading.setWordWrap(True)
        subheading.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font-size: 12px;")
        outer.addWidget(subheading)

        picker_row = QHBoxLayout()
        picker_label = QLabel("Camera:")
        picker_label.setStyleSheet(f"color: {COLOR_TEXT_PRIMARY};")
        picker_row.addWidget(picker_label)
        self.camera_picker = QComboBox()
        self.camera_picker.setStyleSheet(input_style())
        self.camera_picker.currentIndexChanged.connect(self._on_filter_changed)
        picker_row.addWidget(self.camera_picker, stretch=1)

        refresh_btn = QPushButton("Refresh")
        refresh_btn.setStyleSheet(button_style())
        refresh_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        refresh_btn.clicked.connect(self._refresh_table)
        picker_row.addWidget(refresh_btn)
        outer.addLayout(picker_row)

        time_row, self.time_picker, self.from_edit, self.to_edit = _build_time_range_row(
            self, self._on_filter_changed
        )
        outer.addLayout(time_row)

        # Storage summary: this camera's total + all-cameras total,
        # recomputed on every _refresh_table() call (camera switch,
        # manual Refresh, or after a Delete). No auto-refresh timer --
        # same "only as fresh as the last render" tradeoff the Duration
        # column's "so far" values make, see _format_duration.
        self.storage_label = QLabel("")
        self.storage_label.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font-size: 11px;")
        outer.addWidget(self.storage_label)

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["Preview", "Started", "Ended", "Duration", "Size", ""])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        self.table.verticalHeader().setDefaultSectionSize(THUMBNAIL_CELL_H + 6)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.setStyleSheet(table_style())
        outer.addWidget(self.table, stretch=1)

        self.empty_hint = QLabel("No recordings yet for this camera.")
        self.empty_hint.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font-size: 12px;")
        self.empty_hint.setVisible(False)
        outer.addWidget(self.empty_hint)

        self.refresh()

    def refresh(self):
        """Public so SettingsPanel can re-pull data when this section
        becomes visible again -- same convention as every other
        section's refresh()."""
        cameras = self.app.store.list_cameras()
        self.camera_picker.blockSignals(True)
        self.camera_picker.clear()
        for cam in cameras:
            self.camera_picker.addItem(cam["name"], userData=cam["id"])
        self.camera_picker.blockSignals(False)

        if not cameras:
            self.current_camera_id = None
            self._refresh_table()
            return

        idx = 0
        if self.current_camera_id is not None:
            for i, cam in enumerate(cameras):
                if cam["id"] == self.current_camera_id:
                    idx = i
                    break
        self.camera_picker.setCurrentIndex(idx)
        self.current_camera_id = self.camera_picker.itemData(idx)
        self._refresh_table()

    def _on_filter_changed(self, *_args):
        self.current_camera_id = self.camera_picker.currentData()
        self._refresh_table()

    def _refresh_table(self):
        if self.current_camera_id is None:
            self.table.setRowCount(0)
            self.empty_hint.setVisible(True)
            self.storage_label.setText("")
            return

        segments = self.app.event_store.get_segments(self.current_camera_id)

        since_iso, until_iso = _resolve_time_range(self.time_picker, self.from_edit, self.to_edit)
        if since_iso is not None:
            segments = [s for s in segments if s["start_time"] >= since_iso]
        if until_iso is not None:
            segments = [s for s in segments if s["start_time"] <= until_iso]

        self.table.setRowCount(len(segments))
        camera_bytes = 0
        for row, seg in enumerate(segments):
            self.table.setCellWidget(
                row, 0,
                _make_thumbnail_label(
                    thumbnail_path_for(seg["file_path"]),
                    parent_widget=self,
                    title=f"Segment — {_format_iso_for_display(seg['start_time'])}",
                ),
            )
            self.table.setItem(row, 1, QTableWidgetItem(_format_iso_for_display(seg["start_time"])))
            end_display = _format_iso_for_display(seg["end_time"]) if seg["end_time"] else "Recording…"
            self.table.setItem(row, 2, QTableWidgetItem(end_display))
            self.table.setItem(row, 3, QTableWidgetItem(_format_duration(seg["start_time"], seg["end_time"])))

            file_path = seg["file_path"]
            # A segment can outlive its file if retention swept the DB
            # row's file but this query somehow still saw it (a race
            # between the sweep's two steps), or if the file was moved/
            # deleted outside the app -- checked per-row rather than
            # assuming existence, same defensive posture
            # recording_manager.py's own retention sweep already takes.
            file_exists = os.path.exists(file_path)
            is_open = seg["end_time"] is None

            # A still-open segment has no file_size_bytes yet (only
            # written when the segment closes -- see
            # RecordingWorker._close_current_segment). Read the file's
            # current size directly for both display and the storage
            # total, so the row and the summary agree with each other
            # rather than one showing "Recording..." while the other
            # silently under-counts it.
            display_size = seg["file_size_bytes"]
            if display_size is None and file_exists:
                try:
                    display_size = os.path.getsize(file_path)
                except OSError:
                    display_size = None
            self.table.setItem(row, 4, QTableWidgetItem(_format_size(display_size)))
            if display_size:
                camera_bytes += display_size

            play_btn = QPushButton()
            play_btn.setStyleSheet(button_style())
            if is_open:
                # The file exists on disk the whole time it's being
                # written, but an MP4's index (moov atom) is only
                # written when cv2.VideoWriter.release() finalizes it
                # at segment close -- so an in-progress file genuinely
                # isn't playable yet, and clicking Play on it produces
                # a confusing "corrupted or unsupported" error from
                # whatever player opens it. Disabled here instead of
                # letting that happen (see the Recordings QoL follow-up
                # discussion -- MP4/H.264 was kept over AVI/XVID
                # despite this wait, so this is the real fix for the
                # tradeoff that decision implies).
                play_btn.setText("Recording…")
                play_btn.setEnabled(False)
                play_btn.setCursor(Qt.CursorShape.ArrowCursor)
                play_btn.setToolTip(
                    "Still being recorded -- not playable until this segment "
                    "closes (every 30 min)."
                )
            elif file_exists:
                play_btn.setText("Play")
                play_btn.setEnabled(True)
                play_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            else:
                play_btn.setText("Missing")
                play_btn.setEnabled(False)
                play_btn.setCursor(Qt.CursorShape.ArrowCursor)

            folder_btn = QPushButton("Folder")
            folder_btn.setStyleSheet(button_style())
            folder_btn.setCursor(Qt.CursorShape.PointingHandCursor)

            delete_btn = QPushButton("Delete")
            delete_btn.setEnabled(not is_open)
            delete_btn.setStyleSheet(button_style())
            delete_btn.setCursor(
                Qt.CursorShape.PointingHandCursor if not is_open else Qt.CursorShape.ArrowCursor
            )
            if is_open:
                delete_btn.setToolTip("Still recording -- can't delete until this segment closes.")

            # Capture per-row values by default arg, not closure-over-
            # loop-var -- same late-binding fix ZonesSectionPanel's
            # per-row Detect checkboxes already use.
            play_btn.clicked.connect(lambda _checked, path=file_path: self._play(path))
            folder_btn.clicked.connect(lambda _checked, path=file_path: self._open_folder(path))
            delete_btn.clicked.connect(
                lambda _checked, sid=seg["id"], open_=is_open: self._delete_segment(sid, open_)
            )

            cell_container = QWidget()
            cell_layout = QHBoxLayout(cell_container)
            cell_layout.setContentsMargins(4, 2, 4, 2)
            cell_layout.setSpacing(4)
            cell_layout.addWidget(play_btn)
            cell_layout.addWidget(folder_btn)
            cell_layout.addWidget(delete_btn)
            self.table.setCellWidget(row, 5, cell_container)

        self.empty_hint.setVisible(len(segments) == 0)
        self._update_storage_label(camera_bytes)

    def _update_storage_label(self, camera_bytes):
        # All-cameras total: closed segments come straight from the DB
        # (get_total_bytes_all_cameras), then each camera currently
        # mid-recording adds its live, not-yet-persisted file size on
        # top -- one os.path.getsize() call per active camera, not per
        # segment, so this stays cheap regardless of history depth.
        total_bytes = self.app.event_store.get_total_bytes_all_cameras()
        for cam in self.app.store.list_cameras():
            segment_id = self.app.recording.get_current_segment_id(cam["id"])
            if segment_id is None:
                continue
            seg = self.app.event_store.get_segment(segment_id)
            if seg is None or not os.path.exists(seg["file_path"]):
                continue
            try:
                total_bytes += os.path.getsize(seg["file_path"])
            except OSError:
                pass

        self.storage_label.setText(
            f"This camera: {_format_size(camera_bytes)}   •   All cameras: {_format_size(total_bytes)}"
        )

    def _play(self, file_path):
        # Hands off to the OS's default video player rather than an
        # in-app player -- see the Phase 7 design discussion.
        # QUrl.fromLocalFile handles platform path-separator
        # differences (Windows backslashes vs. POSIX forward slashes)
        # so this doesn't need its own per-OS branching.
        opened = QDesktopServices.openUrl(QUrl.fromLocalFile(file_path))
        if not opened:
            QMessageBox.warning(
                self,
                "Couldn't open clip",
                f"Your system didn't open a player for:\n{file_path}",
            )

    def _open_folder(self, file_path):
        folder = os.path.dirname(file_path)
        if not os.path.isdir(folder):
            QMessageBox.warning(self, "Folder not found", f"This folder no longer exists:\n{folder}")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(folder))

    def _delete_segment(self, segment_id, is_open):
        if is_open:
            # Belt-and-suspenders -- the button is already disabled for
            # an open segment, this guards against it being reachable
            # some other way later.
            return
        reply = QMessageBox.question(
            self,
            "Delete recording",
            "Delete this recording permanently? This can't be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        file_path = self.app.event_store.delete_segment(segment_id)
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
            except OSError as exc:
                QMessageBox.warning(
                    self,
                    "Couldn't delete file",
                    f"Removed from the index, but couldn't delete the file on disk:\n{exc}",
                )
        self._refresh_table()


class EventsSectionPanel(QWidget):
    """Phase 7 QoL addition: a unified, persistent history view onto
    event_store's `events` table -- motion start/stop, object
    detections, and alert rule start/stop, all in one place, across
    one camera or all of them.

    This is also what actually exercises
    EventStore.find_segment_for_timestamp() -- each row's "View Clip"
    button looks up whichever recorded segment covers that row's
    timestamp and hands it to the OS's default player. That's the
    exact "given a detection event, can you open the right clip"
    checkpoint the Phase 7 roadmap named; nothing had actually called
    that method until this panel existed.

    Deliberately read-only, same as RecordingsSectionPanel -- there's
    nothing to add/edit here, only ever written by the background
    workers (event_logger.py's motion rows, object_detector.py's
    detection rows via main.py._poll, and alert_manager.py's alert
    rows). Interprets the column-reuse conventions those three modules
    each document (rule name in zone_id / duration in confidence for
    alert rows; actual zone id + real confidence for detection rows)
    -- see _describe_event."""

    TYPE_FILTERS = [
        ("All types", None),
        ("Motion", {"motion_start", "motion_end"}),
        ("Alerts", {"alert_start", "alert_end"}),
        ("Object detections", "object"),  # sentinel: anything NOT in the two sets above
    ]

    def __init__(self, app, parent=None):
        super().__init__(parent)
        self.app = app
        self.current_camera_id = None  # None means "All Cameras"

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 20, 24, 20)
        outer.setSpacing(12)

        heading = QLabel("Events")
        heading.setStyleSheet(f"color: {COLOR_TEXT_PRIMARY}; font-size: 16px; font-weight: bold;")
        outer.addWidget(heading)

        subheading = QLabel(
            "Every motion start/stop, object detection, and alert rule match, "
            "in one history. Click View Clip to open the recording that covers "
            "that moment, if one is still on disk."
        )
        subheading.setWordWrap(True)
        subheading.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font-size: 12px;")
        outer.addWidget(subheading)

        filter_row = QHBoxLayout()
        camera_label = QLabel("Camera:")
        camera_label.setStyleSheet(f"color: {COLOR_TEXT_PRIMARY};")
        filter_row.addWidget(camera_label)
        self.camera_picker = QComboBox()
        self.camera_picker.setStyleSheet(input_style())
        self.camera_picker.currentIndexChanged.connect(self._on_filter_changed)
        filter_row.addWidget(self.camera_picker, stretch=1)

        filter_row.addSpacing(12)
        type_label = QLabel("Type:")
        type_label.setStyleSheet(f"color: {COLOR_TEXT_PRIMARY};")
        filter_row.addWidget(type_label)
        self.type_picker = QComboBox()
        self.type_picker.setStyleSheet(input_style())
        for label, _spec in self.TYPE_FILTERS:
            self.type_picker.addItem(label)
        self.type_picker.currentIndexChanged.connect(self._on_filter_changed)
        filter_row.addWidget(self.type_picker, stretch=1)

        refresh_btn = QPushButton("Refresh")
        refresh_btn.setStyleSheet(button_style())
        refresh_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        refresh_btn.clicked.connect(self._refresh_table)
        filter_row.addWidget(refresh_btn)
        outer.addLayout(filter_row)

        time_row, self.time_picker, self.from_edit, self.to_edit = _build_time_range_row(
            self, self._on_filter_changed
        )
        outer.addLayout(time_row)

        # Class filter -- only meaningfully affects object-detection
        # rows (motion/alert rows have no "class" concept and always
        # pass through regardless of these checkboxes, see
        # _refresh_table). Reuses ObjectDetectionSectionPanel's
        # CANDIDATE_CLASSES list rather than redefining it, so the two
        # stay in sync automatically if that list ever changes.
        class_row = QHBoxLayout()
        class_label = QLabel("Classes:")
        class_label.setStyleSheet(f"color: {COLOR_TEXT_PRIMARY};")
        class_row.addWidget(class_label)

        class_all_btn = QPushButton("All")
        class_none_btn = QPushButton("None")
        for b in (class_all_btn, class_none_btn):
            b.setStyleSheet(f"""
                QPushButton {{
                    background: none;
                    border: none;
                    color: {COLOR_ACCENT};
                    font-size: 11px;
                    padding: 0 4px;
                }}
                QPushButton:hover {{
                    text-decoration: underline;
                }}
            """)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
        class_all_btn.clicked.connect(self._select_all_classes)
        class_none_btn.clicked.connect(self._select_no_classes)
        class_row.addWidget(class_all_btn)
        class_row.addWidget(class_none_btn)
        class_row.addSpacing(8)

        self._class_checkboxes = {}
        for class_name in ObjectDetectionSectionPanel.CANDIDATE_CLASSES:
            cb = QCheckBox(class_name.capitalize())
            cb.setChecked(True)
            cb.setStyleSheet(f"color: {COLOR_TEXT_PRIMARY}; font-size: 11px;")
            cb.toggled.connect(self._on_filter_changed)
            self._class_checkboxes[class_name] = cb
            class_row.addWidget(cb)
        class_row.addStretch(1)
        outer.addLayout(class_row)

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["Preview", "Time", "Camera", "Type", "Detail", ""])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        self.table.verticalHeader().setDefaultSectionSize(THUMBNAIL_CELL_H + 6)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.setStyleSheet(table_style())
        outer.addWidget(self.table, stretch=1)

        self.empty_hint = QLabel("No events logged yet.")
        self.empty_hint.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font-size: 12px;")
        self.empty_hint.setVisible(False)
        outer.addWidget(self.empty_hint)

        self.refresh()

    def refresh(self):
        """Public so SettingsPanel can re-pull data when this section
        becomes visible again -- same convention as every other
        section's refresh(). Preserves the current camera/type filter
        selection across the rebuild where possible."""
        cameras = self.app.store.list_cameras()
        previous = self.camera_picker.currentData() if self.camera_picker.count() else None

        self.camera_picker.blockSignals(True)
        self.camera_picker.clear()
        self.camera_picker.addItem("All Cameras", userData=None)
        for cam in cameras:
            self.camera_picker.addItem(cam["name"], userData=cam["id"])
        idx = self.camera_picker.findData(previous)
        self.camera_picker.setCurrentIndex(idx if idx >= 0 else 0)
        self.camera_picker.blockSignals(False)
        self.current_camera_id = self.camera_picker.currentData()

        self._refresh_table()

    def _on_filter_changed(self, *_args):
        self.current_camera_id = self.camera_picker.currentData()
        self._refresh_table()

    def _select_all_classes(self):
        for cb in self._class_checkboxes.values():
            cb.blockSignals(True)
            cb.setChecked(True)
            cb.blockSignals(False)
        self._refresh_table()

    def _select_no_classes(self):
        for cb in self._class_checkboxes.values():
            cb.blockSignals(True)
            cb.setChecked(False)
            cb.blockSignals(False)
        self._refresh_table()

    def _refresh_table(self):
        since_iso, until_iso = _resolve_time_range(self.time_picker, self.from_edit, self.to_edit)

        events = self.app.event_store.get_events(
            camera_id=self.current_camera_id, since_iso=since_iso, until_iso=until_iso, limit=300,
        )

        _, type_spec = self.TYPE_FILTERS[self.type_picker.currentIndex()]
        if type_spec == "object":
            events = [
                e for e in events
                if e["detection_class"] not in ("motion_start", "motion_end", "alert_start", "alert_end")
            ]
        elif type_spec is not None:
            events = [e for e in events if e["detection_class"] in type_spec]

        # Class filter -- only ever affects object-detection rows.
        # Motion/alert rows have no "class" concept (their
        # detection_class values -- motion_start/motion_end/
        # alert_start/alert_end -- aren't in CANDIDATE_CLASSES at all)
        # so they always pass through here regardless of which
        # checkboxes are ticked; unchecking every class only hides
        # detection rows, never motion or alerts.
        selected_classes = {name for name, cb in self._class_checkboxes.items() if cb.isChecked()}
        special_classes = ("motion_start", "motion_end", "alert_start", "alert_end")
        events = [
            e for e in events
            if e["detection_class"] in special_classes or e["detection_class"] in selected_classes
        ]

        self.table.setRowCount(len(events))
        for row, event in enumerate(events):
            # Only object-detection rows ever have a saved thumbnail
            # (motion/alert rows have no captured image) --
            # _make_thumbnail_label already falls back to a "No
            # preview" placeholder when the path doesn't exist, so no
            # branching needed here.
            self.table.setCellWidget(
                row, 0,
                _make_thumbnail_label(
                    event_thumbnail_path(event["id"]),
                    parent_widget=self,
                    title=f"Event — {_format_iso_for_display(event['detected_at'])}",
                ),
            )
            self.table.setItem(row, 1, QTableWidgetItem(_format_iso_for_display(event["detected_at"])))

            camera = self.app.store.get_camera(event["camera_id"])
            camera_name = camera["name"] if camera else event["camera_id"]
            self.table.setItem(row, 2, QTableWidgetItem(camera_name))

            type_label, detail = self._describe_event(event)
            self.table.setItem(row, 3, QTableWidgetItem(type_label))
            self.table.setItem(row, 4, QTableWidgetItem(detail))

            clip_btn = QPushButton("View Clip")
            clip_btn.setStyleSheet(button_style())
            clip_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            # Capture per-row values by default arg, not closure-over-
            # loop-var -- same late-binding fix used throughout this file.
            clip_btn.clicked.connect(
                lambda _checked, cam_id=event["camera_id"], ts=event["detected_at"]: self._view_clip(cam_id, ts)
            )
            cell_container = QWidget()
            cell_layout = QHBoxLayout(cell_container)
            cell_layout.setContentsMargins(4, 2, 4, 2)
            cell_layout.addWidget(clip_btn)
            self.table.setCellWidget(row, 5, cell_container)

        self.empty_hint.setVisible(len(events) == 0)

    def _describe_event(self, event):
        """Returns (type_label, detail_label) for one event row,
        interpreting the column-reuse conventions documented in
        event_logger.py / alert_manager.py."""
        detection_class = event["detection_class"] or ""

        if detection_class == "motion_start":
            return "Motion started", "—"
        if detection_class == "motion_end":
            return "Motion ended", "—"
        if detection_class == "alert_start":
            return "Alert started", f"Rule: {event['zone_id'] or '—'}"
        if detection_class == "alert_end":
            duration = event["confidence"]
            duration_text = _format_duration_seconds(duration) if duration is not None else "—"
            return "Alert ended", f"Rule: {event['zone_id'] or '—'}  •  lasted {duration_text}"

        # Anything else is an object-detection class name (e.g. "person").
        type_label = f"Detected: {detection_class.capitalize()}" if detection_class else "Event"
        detail_bits = []
        if event["zone_id"]:
            zone = self.app.store.get_zone(event["camera_id"], event["zone_id"])
            detail_bits.append(f"Zone: {zone['name']}" if zone else f"Zone: {event['zone_id']}")
        else:
            detail_bits.append("Whole frame")
        if event["confidence"] is not None:
            detail_bits.append(f"{event['confidence'] * 100:.0f}%")
        return type_label, "  •  ".join(detail_bits)

    def _view_clip(self, camera_id, detected_at_iso):
        """The actual exercise of find_segment_for_timestamp() -- see
        the class docstring. Looks up whichever segment covers this
        event's timestamp and hands it to the OS's default player,
        same _play pattern RecordingsSectionPanel uses."""
        segment = self.app.event_store.find_segment_for_timestamp(camera_id, detected_at_iso)
        if segment is None:
            QMessageBox.information(
                self,
                "No recording found",
                "No recorded segment covers this moment -- it may predate when "
                "recording started for this camera, or the segment has since "
                "been removed by retention.",
            )
            return

        file_path = segment["file_path"]
        if not os.path.exists(file_path):
            QMessageBox.warning(
                self,
                "Recording missing",
                "The segment that covered this moment is no longer on disk.",
            )
            return

        opened = QDesktopServices.openUrl(QUrl.fromLocalFile(file_path))
        if not opened:
            QMessageBox.warning(
                self,
                "Couldn't open clip",
                f"Your system didn't open a player for:\n{file_path}",
            )


class SettingsPanel(QWidget):
    """Sidebar + stacked sections shell. MainWindow swaps this into its
    view stack as a full page (see show_settings_view in main.py).

    To add a new section later: write a QWidget subclass for it (giving
    it a `refresh()` method if its data can go stale while not visible
    is a good idea, though not required), then add one line to
    _build_sections() below. Nothing else needs to change.
    """

    def __init__(self, app, parent=None):
        super().__init__(parent)
        self.app = app

        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self.sidebar = QListWidget()
        self.sidebar.setStyleSheet(sidebar_style())
        self.sidebar.setFixedWidth(180)
        self.sidebar.currentRowChanged.connect(self._on_section_changed)
        outer.addWidget(self.sidebar)

        self.stack = QStackedWidget()
        self.stack.setStyleSheet(f"background-color: {COLOR_BG};")
        outer.addWidget(self.stack, stretch=1)

        self._sections = []  # list of (label, panel_widget)
        self._build_sections()

        if self.sidebar.count() > 0:
            self.sidebar.setCurrentRow(0)

    def _build_sections(self):
        # Registry of sections -- add new ones here as the app grows
        # (Account, etc. in later phases).
        self._add_section("Cameras", CamerasSectionPanel(self.app))
        self._add_section("Zones", ZonesSectionPanel(self.app))
        self._add_section("Object Detection", ObjectDetectionSectionPanel(self.app))
        self._add_section("Alerts", AlertsSectionPanel(self.app))
        self._add_section("Recordings", RecordingsSectionPanel(self.app))
        self._add_section("Events", EventsSectionPanel(self.app))

    def _add_section(self, label, panel_widget):
        self._sections.append((label, panel_widget))
        self.sidebar.addItem(QListWidgetItem(label))
        self.stack.addWidget(panel_widget)

    def _on_section_changed(self, row):
        if row < 0:
            return
        self.stack.setCurrentIndex(row)
        _, panel = self._sections[row]
        if hasattr(panel, "refresh"):
            panel.refresh()

    def refresh_current_section(self):
        """Call when re-entering the settings page, in case data
        changed elsewhere (e.g. a camera was removed while Settings
        wasn't visible)."""
        row = self.sidebar.currentRow()
        if row >= 0:
            self._on_section_changed(row)

    def get_zones_panel(self):
        """Direct accessor for cross-section sync (MainWindow calls
        this from notify_zones_changed so a zone edited live in
        SingleView shows up here immediately if this section happens
        to be open on the same camera)."""
        for label, panel in self._sections:
            if label == "Zones":
                return panel
        return None