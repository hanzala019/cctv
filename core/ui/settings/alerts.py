"""
Settings > Alerts: per-camera alert rules.

Rule matching itself lives in core.alerts.alert_matcher, which is pure
logic with no I/O -- that is where the time-window behaviour
(including windows that cross midnight) is defined and tested.
"""

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

from core.ui.settings.alert_dialog import AlertRuleEditDialog
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
