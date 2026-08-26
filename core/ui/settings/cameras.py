"""Settings > Cameras: the camera list, add / edit / delete."""

from datetime import datetime, timedelta

from PyQt6.QtCore import QDateTime, Qt
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDateTimeEdit,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.ui.settings.camera_dialog import CameraEditDialog
from core.ui.theme import (
    COLOR_TEXT_MUTED,
    COLOR_TEXT_PRIMARY,
    button_style,
    input_style,
    table_style,
)

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
