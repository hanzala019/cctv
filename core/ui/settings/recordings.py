"""
Settings > Recordings: browse recorded segments and open them.

Recording is currently unconditional for every camera with a fixed
retention window. See README.md, "Known issues".
"""

import os
from datetime import datetime, timedelta

from PyQt6.QtCore import QDateTime, Qt, QUrl
from PyQt6.QtGui import QDesktopServices
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

from core.recording.recording_manager import thumbnail_path_for
from core.ui.settings.dialogs import _make_thumbnail_label
from core.ui.settings.formatting import _format_duration, _format_iso_for_display, _format_size
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
