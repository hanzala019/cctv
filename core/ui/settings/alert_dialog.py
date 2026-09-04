"""Add / edit dialog for a single alert rule."""

from datetime import datetime, timedelta

from PyQt6.QtCore import QDateTime, QTime
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDateTimeEdit,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTimeEdit,
    QVBoxLayout,
)

from core.ui.settings.object_detection import ObjectDetectionSectionPanel
from core.ui.theme import (
    COLOR_PANEL_BG,
    COLOR_STATUS_CONNECTING,
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
