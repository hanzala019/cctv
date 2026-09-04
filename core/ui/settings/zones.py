"""
Settings > Zones: draw and manage per-camera detection zones.

Zone points are normalised 0.0-1.0 against the source frame, never
against the editor canvas, so a zone survives a resolution change.
"""

from datetime import datetime, timedelta

from PyQt6.QtCore import QDateTime, Qt
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDateTimeEdit,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.ui.theme import (
    COLOR_BORDER,
    COLOR_CAPTION_BG,
    COLOR_TEXT_MUTED,
    COLOR_TEXT_PRIMARY,
    button_style,
    input_style,
    table_style,
)
from core.ui.views.zone_editor import ZoneEditorView

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
