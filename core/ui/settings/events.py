"""Settings > Events: browse logged motion and detection events."""

import os
from datetime import datetime, timedelta

from PyQt6.QtCore import QDateTime, Qt, QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
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

from core.storage.event_store import event_thumbnail_path
from core.ui.settings.dialogs import _make_thumbnail_label
from core.ui.settings.formatting import _format_duration_seconds, _format_iso_for_display
from core.ui.settings.object_detection import ObjectDetectionSectionPanel
from core.ui.theme import (
    COLOR_ACCENT,
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
            # An event's camera may since have been deleted -- get_zone
            # raises ValueError for an unknown camera, correct for live
            # Settings UI flows but wrong here: an orphaned historical
            # event is expected, not an error. Falls back to the raw
            # zone id if the camera (or zone) no longer exists.
            zone = None
            if self.app.store.get_camera(event["camera_id"]) is not None:
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
