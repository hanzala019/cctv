"""
Add / edit dialog for a single camera.

Note: the URL field currently shows RTSP credentials in clear text.
See README.md, "Known issues".
"""

from datetime import datetime, timedelta

from PyQt6.QtCore import QDateTime
from PyQt6.QtWidgets import (
    QComboBox,
    QDateTimeEdit,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from core.ui.theme import (
    COLOR_PANEL_BG,
    COLOR_TEXT_MUTED,
    COLOR_TEXT_PRIMARY,
    button_style,
    input_style,
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
