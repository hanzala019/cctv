"""
The side panel listing recent object detections, with thumbnails.

Thumbnails are read from disk by deterministic path (see
event_store.event_thumbnail_path) rather than stored in the database.
"""

import time

from PyQt6.QtCore import QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QIcon, QPixmap
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from core.ui.settings import ObjectDetectionSectionPanel
from core.ui.theme import (
    COLOR_ACCENT,
    COLOR_BORDER,
    COLOR_CAPTION_BG,
    COLOR_PANEL_BG,
    COLOR_TEXT_MUTED,
    COLOR_TEXT_PRIMARY,
)
from core.ui.theme import button_style as _button_style
from core.ui.theme import input_style as _input_style

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


class _DetectionPreviewDialog(QDialog):
    """Enlarged preview shown when a DetectionSidePanel entry is
    clicked -- see DetectionSidePanel._on_item_clicked for why this
    replaced immediate navigation. "Go to Camera" accepts the dialog
    (the caller checks exec()'s return value to decide whether to
    navigate); Close/Esc just dismisses it with no navigation."""

    def __init__(self, thumbnail_bytes, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Detection preview")
        self.setStyleSheet(f"background-color: {COLOR_PANEL_BG}; color: {COLOR_TEXT_PRIMARY};")

        layout = QVBoxLayout(self)

        label = QLabel()
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        pixmap = QPixmap()
        if thumbnail_bytes and pixmap.loadFromData(thumbnail_bytes):
            scaled = pixmap.scaled(
                480, 360, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation,
            )
            label.setPixmap(scaled)
        else:
            label.setText("No preview available for this event.")
            label.setStyleSheet(f"color: {COLOR_TEXT_MUTED};")
        layout.addWidget(label)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        close_btn = QPushButton("Close")
        close_btn.setStyleSheet(_button_style())
        close_btn.clicked.connect(self.reject)
        goto_btn = QPushButton("Go to Camera")
        goto_btn.setStyleSheet(_button_style())
        goto_btn.clicked.connect(self.accept)
        btn_row.addWidget(close_btn)
        btn_row.addWidget(goto_btn)
        layout.addLayout(btn_row)


class DetectionSidePanel(QWidget):
    """Persistent right-side panel listing object-detection events live
    as they happen -- fresh "detected" lines and periodic "still here"
    confirmations, newest at the bottom. Lives as a sibling to
    MainWindow's grid/single/settings QStackedWidget rather than inside
    it, so it stays visible (or collapsed) independent of whichever
    page is currently showing -- toggled via the toolbar's "Detections"
    button (MainWindow.detections_btn), same interaction pattern as the
    Settings button, plus its own in-panel Hide button. Width is capped
    at 15% of the window's width by MainWindow._update_detection_panel_width
    (called on init and on every resize) so it never crowds out the
    video views.

    Filters (camera / class / time range) narrow what's *displayed*,
    not what's retained -- self._events holds every event this panel
    has received (capped at MAX_VISIBLE_ITEMS, same bound as before),
    and the QListWidget is rebuilt from that against the current filter
    whenever a filter control changes. New events take a fast path
    (append directly if they pass the current filter) rather than a
    full rebuild, except for the periodic prune tick a relative time
    window needs (see _prune_timer) -- without new events arriving, a
    "Last 5 min" filter still needs to shed items as they age out.

    Purely an in-app view onto ObjectDetectionManager's bounded
    in-memory log; Phase 4 has no persistence (Phase 7 adds a real
    SQLite-backed event history this could read from instead)."""

    MAX_VISIBLE_ITEMS = 500
    WIDTH_FRACTION = 0.15
    MIN_WIDTH = 220
    ICON_SIZE = QSize(64, 48)
    PRUNE_INTERVAL_MS = 10_000  # only matters while a relative time-range filter is active

    # (display label, cutoff spec) -- None means no cutoff (all time),
    # "today" means local midnight, an int means "now minus N seconds".
    TIME_RANGE_OPTIONS = [
        ("All time", None),
        ("Last 5 min", 5 * 60),
        ("Last 15 min", 15 * 60),
        ("Last 1 hour", 60 * 60),
        ("Today", "today"),
    ]

    hideRequested = pyqtSignal()
    eventActivated = pyqtSignal(str)  # camera_id, emitted when a log entry is clicked

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet(f"background-color: {COLOR_PANEL_BG}; border-left: 1px solid {COLOR_BORDER};")

        self._events = []  # every DetectionEvent received, capped at MAX_VISIBLE_ITEMS
        self._camera_filter = None  # cam_id, or None for "All Cameras"
        self._class_filter = set(ObjectDetectionSectionPanel.CANDIDATE_CLASSES)  # all checked by default

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        header = QFrame()
        header.setStyleSheet(f"background-color: {COLOR_PANEL_BG}; border-bottom: 1px solid {COLOR_BORDER};")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(10, 8, 10, 8)

        title = QLabel("Detections")
        title_font = QFont()
        title_font.setBold(True)
        title_font.setPointSize(10)
        title.setFont(title_font)
        title.setStyleSheet(f"color: {COLOR_TEXT_PRIMARY};")
        header_layout.addWidget(title)
        header_layout.addStretch(1)

        hide_btn = QPushButton("Hide")
        hide_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        hide_btn.setStyleSheet(_button_style())
        hide_btn.clicked.connect(self.hideRequested.emit)
        header_layout.addWidget(hide_btn)

        outer.addWidget(header, stretch=0)

        # ---- filters: camera, time range, class checkboxes -----------
        filters_frame = QFrame()
        filters_frame.setStyleSheet(f"background-color: {COLOR_PANEL_BG}; border-bottom: 1px solid {COLOR_BORDER};")
        filters_layout = QVBoxLayout(filters_frame)
        filters_layout.setContentsMargins(10, 8, 10, 8)
        filters_layout.setSpacing(6)

        camera_row = QHBoxLayout()
        camera_label = QLabel("Camera:")
        camera_label.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font-size: 11px;")
        camera_row.addWidget(camera_label)
        self.camera_combo = QComboBox()
        self.camera_combo.setStyleSheet(_input_style())
        self.camera_combo.addItem("All Cameras", userData=None)
        self.camera_combo.currentIndexChanged.connect(self._on_filter_changed)
        camera_row.addWidget(self.camera_combo, stretch=1)
        filters_layout.addLayout(camera_row)

        time_row = QHBoxLayout()
        time_label = QLabel("Time:")
        time_label.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font-size: 11px;")
        time_row.addWidget(time_label)
        self.time_combo = QComboBox()
        self.time_combo.setStyleSheet(_input_style())
        for label, _cutoff_spec in self.TIME_RANGE_OPTIONS:
            self.time_combo.addItem(label)
        self.time_combo.currentIndexChanged.connect(self._on_filter_changed)
        time_row.addWidget(self.time_combo, stretch=1)
        filters_layout.addLayout(time_row)

        classes_header_row = QHBoxLayout()
        classes_label = QLabel("Classes:")
        classes_label.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font-size: 11px;")
        classes_header_row.addWidget(classes_label)
        classes_header_row.addStretch(1)
        all_btn = QPushButton("All")
        none_btn = QPushButton("None")
        for b in (all_btn, none_btn):
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
        all_btn.clicked.connect(self._select_all_classes)
        none_btn.clicked.connect(self._select_no_classes)
        classes_header_row.addWidget(all_btn)
        classes_header_row.addWidget(none_btn)
        filters_layout.addLayout(classes_header_row)

        classes_grid = QHBoxLayout()
        col_a = QVBoxLayout()
        col_b = QVBoxLayout()
        self._class_checkboxes = {}
        for i, class_name in enumerate(ObjectDetectionSectionPanel.CANDIDATE_CLASSES):
            cb = QCheckBox(class_name.capitalize())
            cb.setChecked(True)
            cb.setStyleSheet(f"color: {COLOR_TEXT_PRIMARY}; font-size: 11px;")
            cb.toggled.connect(self._on_filter_changed)
            self._class_checkboxes[class_name] = cb
            (col_a if i % 2 == 0 else col_b).addWidget(cb)
        classes_grid.addLayout(col_a)
        classes_grid.addLayout(col_b)
        classes_grid.addStretch(1)
        filters_layout.addLayout(classes_grid)

        outer.addWidget(filters_frame, stretch=0)

        hint = QLabel("Click an entry to jump to that camera.")
        hint.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font-size: 10px; padding: 4px 8px;")
        outer.addWidget(hint)

        self.list_widget = QListWidget()
        self.list_widget.setWordWrap(True)
        self.list_widget.setIconSize(self.ICON_SIZE)
        self.list_widget.setCursor(Qt.CursorShape.PointingHandCursor)
        self.list_widget.setStyleSheet(f"""
            QListWidget {{
                background-color: {COLOR_CAPTION_BG};
                color: {COLOR_TEXT_PRIMARY};
                border: none;
            }}
            QListWidget::item {{
                padding: 5px 8px;
            }}
            QListWidget::item:hover {{
                background-color: {COLOR_PANEL_BG};
            }}
        """)
        self.list_widget.itemClicked.connect(self._on_item_clicked)
        outer.addWidget(self.list_widget, stretch=1)

        # Only matters while a relative ("Last N min") time filter is
        # active -- items need to age out even with no new events
        # arriving to trigger a rebuild otherwise.
        self._prune_timer = QTimer(self)
        self._prune_timer.timeout.connect(self._on_prune_tick)
        self._prune_timer.start(self.PRUNE_INTERVAL_MS)

    # ----- camera list sync (called by MainWindow) ----------------------

    def set_cameras(self, cameras):
        """cameras: list of camera dicts from CameraStore.list_cameras().
        Called by MainWindow on startup and whenever cameras are added,
        removed, or renamed, so the filter dropdown stays current.
        Preserves the current selection if that camera still exists."""
        current = self.camera_combo.currentData()
        self.camera_combo.blockSignals(True)
        self.camera_combo.clear()
        self.camera_combo.addItem("All Cameras", userData=None)
        for cam in cameras:
            self.camera_combo.addItem(cam["name"], userData=cam["id"])
        idx = self.camera_combo.findData(current)
        self.camera_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.camera_combo.blockSignals(False)
        self._camera_filter = self.camera_combo.currentData()

    # ----- filter state + rebuild ---------------------------------------

    def _select_all_classes(self):
        for cb in self._class_checkboxes.values():
            cb.blockSignals(True)
            cb.setChecked(True)
            cb.blockSignals(False)
        self._on_filter_changed()

    def _select_no_classes(self):
        for cb in self._class_checkboxes.values():
            cb.blockSignals(True)
            cb.setChecked(False)
            cb.blockSignals(False)
        self._on_filter_changed()

    def _on_filter_changed(self, *_args):
        self._camera_filter = self.camera_combo.currentData()
        self._class_filter = {name for name, cb in self._class_checkboxes.items() if cb.isChecked()}
        self._rebuild_list()

    def _on_prune_tick(self):
        _label, cutoff_spec = self.TIME_RANGE_OPTIONS[self.time_combo.currentIndex()]
        if cutoff_spec is None or cutoff_spec == "today":
            return  # nothing ages out of "all time" or "today" between ticks
        self._rebuild_list()

    def _compute_time_cutoff(self):
        _label, cutoff_spec = self.TIME_RANGE_OPTIONS[self.time_combo.currentIndex()]
        if cutoff_spec is None:
            return None
        if cutoff_spec == "today":
            now = time.localtime()
            midnight = (now.tm_year, now.tm_mon, now.tm_mday, 0, 0, 0, now.tm_wday, now.tm_yday, now.tm_isdst)
            return time.mktime(time.struct_time(midnight))
        return time.time() - cutoff_spec

    def _passes_filters(self, event, time_cutoff):
        if self._camera_filter is not None and event.camera_id != self._camera_filter:
            return False
        if event.class_name not in self._class_filter:
            return False
        if time_cutoff is not None and event.timestamp < time_cutoff:
            return False
        return True

    def _append_item(self, event):
        """Builds and adds one QListWidgetItem for an event that's
        already passed the current filter -- shared by the fast
        append-only path (add_events) and the full rebuild path."""
        item = QListWidgetItem(event.message())
        item.setData(Qt.ItemDataRole.UserRole, event.camera_id)
        # Thumbnail bytes stored a second time here (UserRole + 1),
        # separate from the small icon rendered in the list itself --
        # this is what lets _on_item_clicked show an enlarged preview
        # without needing to re-fetch anything.
        item.setData(Qt.ItemDataRole.UserRole + 1, event.thumbnail)
        if not event.is_new:
            item.setForeground(QColor(COLOR_TEXT_MUTED))
        if event.thumbnail:
            pixmap = QPixmap()
            if pixmap.loadFromData(event.thumbnail):
                item.setIcon(QIcon(pixmap))
        self.list_widget.addItem(item)

    def _rebuild_list(self):
        """Full re-render of the visible list from self._events against
        the current filter state. Called when a filter control changes,
        and periodically by _prune_timer for relative time windows."""
        self.list_widget.clear()
        cutoff = self._compute_time_cutoff()
        for event in self._events:
            if self._passes_filters(event, cutoff):
                self._append_item(event)
        self.list_widget.scrollToBottom()

    def _on_item_clicked(self, item):
        cam_id = item.data(Qt.ItemDataRole.UserRole)
        if not cam_id:
            return
        thumbnail_bytes = item.data(Qt.ItemDataRole.UserRole + 1)
        # QoL fix: this used to navigate to live view immediately on
        # any click, which made the thumbnail effectively unviewable
        # at a useful size -- there was no way to look at it without
        # also leaving the panel. Now a click opens an enlarged
        # preview instead, with an explicit "Go to Camera" action for
        # anyone who does want to jump over (dialog.exec() only
        # returns Accepted if that button was clicked, not Close/Esc).
        dialog = _DetectionPreviewDialog(thumbnail_bytes, parent=self)
        if dialog.exec():
            self.eventActivated.emit(cam_id)

    def add_events(self, events):
        """events: list of object_detector.DetectionEvent, oldest
        first. Retained in self._events (capped) regardless of filter
        state, so switching filters later can still surface them.
        "Still here" confirmations render dimmer than fresh detections
        so a scrolling log still reads at a glance which lines are new
        information vs. a repeat confirmation. Each item carries the
        source camera_id (for click-to-jump) and a small thumbnail icon
        when the event has one."""
        if not events:
            return

        self._events.extend(events)
        if len(self._events) > self.MAX_VISIBLE_ITEMS:
            self._events = self._events[-self.MAX_VISIBLE_ITEMS:]

        cutoff = self._compute_time_cutoff()
        appended_any = False
        for event in events:
            if self._passes_filters(event, cutoff):
                self._append_item(event)
                appended_any = True
        if appended_any:
            self.list_widget.scrollToBottom()
