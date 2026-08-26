"""
Settings > Object Detection: per-camera class selection, per-class
confidence thresholds, and the detection mode (on-motion vs
continuous).
"""

from datetime import datetime, timedelta

from PyQt6.QtCore import QDateTime
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDateTimeEdit,
    QHBoxLayout,
    QLabel,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from core.ui.theme import (
    COLOR_STATUS_CONNECTING,
    COLOR_TEXT_MUTED,
    COLOR_TEXT_PRIMARY,
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


class ObjectDetectionSectionPanel(QWidget):
    """Phase 4 settings section: per-camera object detection tuning --
    master on/off, trigger mode (on-motion vs continuous), and which
    COCO classes to keep. Deliberately simpler than ZonesSectionPanel
    -- there's no canvas here, since there's nothing spatial to edit;
    cropping happens automatically off each zone's existing geometry
    in object_detector.py. Unlike zone/motion changes, nothing here
    needs to route through app.notify_zones_changed -- the detection
    worker re-reads camera settings straight from the store on every
    tick rather than caching them, so a plain store write is enough
    for the change to take effect on the next tick.
    """

    CANDIDATE_CLASSES = [
        "person", "car", "truck", "bus", "motorcycle",
        "bicycle", "dog", "cat",
    ]

    MODE_LABELS = [("On motion", "on_motion"), ("Continuous", "continuous")]

    def __init__(self, app, parent=None):
        super().__init__(parent)
        self.app = app
        self.current_camera_id = None
        self._class_checkboxes = {}  # class_name -> QCheckBox
        self._class_confidence_spinboxes = {}  # class_name -> QSpinBox (percent)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 20, 24, 20)
        outer.setSpacing(12)

        heading = QLabel("Object Detection")
        heading.setStyleSheet(f"color: {COLOR_TEXT_PRIMARY}; font-size: 16px; font-weight: bold;")
        outer.addWidget(heading)

        subheading = QLabel(
            "Classifies what triggered motion using YOLOv8n. By default "
            "it only runs when a zone (or the whole frame, for cameras "
            "with no zones opted into detection) flags motion -- cropped "
            "to that zone plus some padding for context, with a 1s "
            "cooldown between repeat checks of the same area. Switch to "
            "Continuous to run on a fixed interval regardless of motion. "
            "Each class below tracks multiple simultaneous instances "
            "independently -- 2 people and a car all showing up at once "
            "are reported separately, not collapsed into one detection."
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

        self.enabled_check = QCheckBox("Enable object detection for this camera")
        self.enabled_check.setStyleSheet(f"color: {COLOR_TEXT_PRIMARY};")
        self.enabled_check.toggled.connect(self._on_enabled_toggled)
        outer.addWidget(self.enabled_check)

        mode_row = QHBoxLayout()
        mode_label = QLabel("Trigger mode:")
        mode_label.setStyleSheet(f"color: {COLOR_TEXT_PRIMARY};")
        mode_row.addWidget(mode_label)
        self.mode_combo = QComboBox()
        self.mode_combo.setStyleSheet(input_style())
        for display_label, _value in self.MODE_LABELS:
            self.mode_combo.addItem(display_label)
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        mode_row.addWidget(self.mode_combo)
        mode_row.addStretch(1)
        outer.addLayout(mode_row)

        classes_label = QLabel("Classes to detect:")
        classes_label.setStyleSheet(f"color: {COLOR_TEXT_PRIMARY}; margin-top: 6px;")
        outer.addWidget(classes_label)

        classes_hint = QLabel(
            "Only checked classes are logged. The percentage is this "
            "camera's own minimum confidence for that class -- e.g. "
            "\"Person\" at 70% on a busy street camera to cut down false "
            "positives, but \"Car\" at 40% on the same camera since cars "
            "are rarely misclassified. Only matters while the class is "
            "checked."
        )
        classes_hint.setWordWrap(True)
        classes_hint.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font-size: 11px;")
        outer.addWidget(classes_hint)

        classes_grid = QHBoxLayout()
        col_a = QVBoxLayout()
        col_b = QVBoxLayout()
        for i, class_name in enumerate(self.CANDIDATE_CLASSES):
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)

            checkbox = QCheckBox(class_name.capitalize())
            checkbox.setStyleSheet(f"color: {COLOR_TEXT_PRIMARY};")
            checkbox.toggled.connect(self._on_class_toggled)
            self._class_checkboxes[class_name] = checkbox
            row.addWidget(checkbox)

            conf_spin = QSpinBox()
            conf_spin.setRange(1, 99)
            conf_spin.setSuffix("%")
            conf_spin.setStyleSheet(input_style())
            conf_spin.setFixedWidth(70)
            conf_spin.setEnabled(False)  # only meaningful once this class is checked
            conf_spin.valueChanged.connect(
                lambda value, cls=class_name: self._on_class_confidence_changed(cls, value)
            )
            self._class_confidence_spinboxes[class_name] = conf_spin
            row.addWidget(conf_spin)
            row.addStretch(1)

            row_container = QWidget()
            row_container.setLayout(row)
            (col_a if i % 2 == 0 else col_b).addWidget(row_container)
        classes_grid.addLayout(col_a)
        classes_grid.addLayout(col_b)
        classes_grid.addStretch(1)
        outer.addLayout(classes_grid)

        # Quality-of-life: without this, enabling detection with every
        # class unchecked is a silent no-op -- the worker skips
        # inference entirely (see object_detector.py's allowed_classes
        # check) with no feedback anywhere that anything's wrong.
        self.no_classes_warning = QLabel(
            "⚠ No classes selected -- detection is enabled but nothing will be logged."
        )
        self.no_classes_warning.setWordWrap(True)
        self.no_classes_warning.setStyleSheet(f"color: {COLOR_STATUS_CONNECTING}; font-size: 11px;")
        self.no_classes_warning.setVisible(False)
        outer.addWidget(self.no_classes_warning)

        outer.addStretch(1)

        self.refresh()

    def refresh(self):
        """Public so SettingsPanel can re-pull data when this section
        becomes visible again (e.g. after a camera was added/removed
        elsewhere)."""
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
        """Switch the panel to a different camera -- signals blocked
        while populating controls to match stored state, since that's
        not itself a user edit and shouldn't trigger a save (same
        pattern as ZonesSectionPanel._load_camera)."""
        self.current_camera_id = cam_id
        controls_enabled = cam_id is not None
        self.enabled_check.setEnabled(controls_enabled)
        self.mode_combo.setEnabled(controls_enabled)
        for cb in self._class_checkboxes.values():
            cb.setEnabled(controls_enabled)

        if cam_id is None:
            self.enabled_check.blockSignals(True)
            self.enabled_check.setChecked(False)
            self.enabled_check.blockSignals(False)
            for name, cb in self._class_checkboxes.items():
                cb.blockSignals(True)
                cb.setChecked(False)
                cb.blockSignals(False)
                spin = self._class_confidence_spinboxes[name]
                spin.setEnabled(False)
            self._update_no_classes_warning()
            return

        enabled = self.app.store.get_object_detection_enabled(cam_id)
        mode = self.app.store.get_object_detection_mode(cam_id)
        classes = set(self.app.store.get_object_detection_classes(cam_id))

        self.enabled_check.blockSignals(True)
        self.enabled_check.setChecked(enabled)
        self.enabled_check.blockSignals(False)

        self.mode_combo.blockSignals(True)
        mode_values = [value for _label, value in self.MODE_LABELS]
        try:
            idx = mode_values.index(mode)
        except ValueError:
            idx = mode_values.index("on_motion")
        self.mode_combo.setCurrentIndex(idx)
        self.mode_combo.blockSignals(False)

        for class_name, cb in self._class_checkboxes.items():
            is_checked = class_name in classes
            cb.blockSignals(True)
            cb.setChecked(is_checked)
            cb.blockSignals(False)

            spin = self._class_confidence_spinboxes[class_name]
            threshold = self.app.store.get_class_confidence(cam_id, class_name)
            spin.blockSignals(True)
            spin.setValue(round(threshold * 100))
            spin.blockSignals(False)
            spin.setEnabled(is_checked)

        self._update_no_classes_warning()

    def _update_no_classes_warning(self):
        """Shows the warning only when detection is enabled for the
        current camera AND every class checkbox is unchecked -- that
        combination is a silent no-op in object_detector.py otherwise."""
        if self.current_camera_id is None:
            self.no_classes_warning.setVisible(False)
            return
        enabled = self.enabled_check.isChecked()
        any_checked = any(cb.isChecked() for cb in self._class_checkboxes.values())
        self.no_classes_warning.setVisible(enabled and not any_checked)

    def _on_enabled_toggled(self, checked):
        if self.current_camera_id is None:
            return
        self.app.store.set_object_detection_enabled(self.current_camera_id, checked)
        self._update_no_classes_warning()

    def _on_mode_changed(self, index):
        if self.current_camera_id is None or index < 0:
            return
        _label, value = self.MODE_LABELS[index]
        self.app.store.set_object_detection_mode(self.current_camera_id, value)

    def _on_class_toggled(self, _checked):
        if self.current_camera_id is None:
            return
        selected = [name for name, cb in self._class_checkboxes.items() if cb.isChecked()]
        self.app.store.set_object_detection_classes(self.current_camera_id, selected)
        for name, spin in self._class_confidence_spinboxes.items():
            spin.setEnabled(self._class_checkboxes[name].isChecked())
        self._update_no_classes_warning()

    def _on_class_confidence_changed(self, class_name, percent_value):
        if self.current_camera_id is None:
            return
        self.app.store.set_class_confidence_threshold(
            self.current_camera_id, class_name, percent_value / 100.0
        )
