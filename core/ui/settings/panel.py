"""
The Settings shell: the section list on the left and the stacked
section panels on the right.

Adding a section is three lines -- a new module, one entry here, one
export in __init__.py. Never edit another section's file to add yours.
"""

from datetime import datetime, timedelta

from PyQt6.QtCore import QDateTime
from PyQt6.QtWidgets import (
    QComboBox,
    QDateTimeEdit,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QStackedWidget,
    QWidget,
)

from core.ui.settings.alerts import AlertsSectionPanel
from core.ui.settings.cameras import CamerasSectionPanel
from core.ui.settings.events import EventsSectionPanel
from core.ui.settings.object_detection import ObjectDetectionSectionPanel
from core.ui.settings.recordings import RecordingsSectionPanel
from core.ui.settings.zones import ZonesSectionPanel
from core.ui.theme import (
    COLOR_BG,
    COLOR_TEXT_PRIMARY,
    input_style,
    sidebar_style,
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
