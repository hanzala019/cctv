"""
Dialogs and helpers shared by more than one Settings section.

Anything used by exactly one section belongs in that section's file,
not here -- this module exists to avoid duplication, not to be a
dumping ground.
"""

import os
from datetime import datetime, timedelta

from PyQt6.QtCore import QDateTime, Qt
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import (
    QComboBox,
    QDateTimeEdit,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

from core.ui.theme import (
    COLOR_CAPTION_BG,
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
