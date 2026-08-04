"""
main.py (PyQt6)

GUI for the CCTV viewer. This replaces the previous Tkinter implementation
with PyQt6 -- camera_store.py, video_stream.py, and grid_layout.py are
unchanged and have no GUI dependency either way.

Visual design notes:
- Dark theme throughout (monitoring software is almost always viewed in
  dim rooms; a bright UI fights the video for attention and is harsh on
  the eyes during long sessions).
- Every tile -- in the grid AND in single-camera view -- shows the
  camera's assigned name in a caption bar, plus a status indicator that
  only appears when something needs attention (connecting/disconnected).
  A camera that's working quietly shows just its name, no clutter.
- Phase 4 adds a standalone "Detections" dock (outside Settings) that
  lists object-detection events live as they happen -- see
  DetectionLogDock below.
- Phase 7 adds always-on local recording (RecordingManager) and a
  SQLite event/segment index (EventStore) -- see the Phase 7 block in
  MainWindow.__init__ and _log_detection_events below. Deliberately no
  new Settings section: recording is always-on for every camera, no
  per-camera switch, no UI-configurable segment length or retention.
"""

import sys
import time
from datetime import datetime

import cv2
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QRect, QRectF, QPointF, QSize
from PyQt6.QtGui import QImage, QPixmap, QColor, QPalette, QFont, QPainter, QPen, QBrush, QPolygonF, QIcon
from PyQt6.QtWidgets import (
    QApplication,
    QWidget,
    QMainWindow,
    QDialog,
    QMessageBox,
    QVBoxLayout,
    QHBoxLayout,
    QGridLayout,
    QLabel,
    QPushButton,
    QFrame,
    QStackedWidget,
    QInputDialog,
    QListWidget,
    QListWidgetItem,
    QComboBox,
    QCheckBox,
)

from camera_store import CameraStore
from video_stream import StreamManager, StreamStatus
from motion_detector import MotionManager
from object_detector import ObjectDetectionManager
from alert_manager import AlertManager
from event_store import EventStore, save_event_thumbnail
from recording_manager import RecordingManager, RecordingStatus
from event_logger import EventLoggerManager
from grid_layout import grid_dimensions, tile_positions
from zone_editor import ZoneEditorView
from ui_theme import (
    COLOR_BG,
    COLOR_TILE_BG,
    COLOR_CAPTION_BG,
    COLOR_CAPTION_BORDER,
    COLOR_TEXT_PRIMARY,
    COLOR_TEXT_MUTED,
    COLOR_STATUS_CONNECTING,
    COLOR_STATUS_ERROR,
    COLOR_ACCENT,
    COLOR_PANEL_BG,
    COLOR_BORDER,
    COLOR_ZONE_OVERLAY_STROKE,
    COLOR_ZONE_OVERLAY_LABEL_BG,
    COLOR_ZONE_OVERLAY_LABEL_TEXT,
    COLOR_MOTION_ACTIVE,
    COLOR_ZONE_MOTION_STROKE,
    COLOR_ZONE_MOTION_FILL,
    COLOR_DETECTION_BOX_STROKE,
    COLOR_DETECTION_BOX_LABEL_BG,
    COLOR_DETECTION_BOX_LABEL_TEXT,
    button_style as _button_style,
    input_style as _input_style,
)
from settings_panel import SettingsPanel, ObjectDetectionSectionPanel

POLL_INTERVAL_MS = 33  # ~30 FPS UI refresh rate

MOTION_ICON_SIZE = 16  # caption bar is small; keep the glyph compact

# How long a detection's bounding box stays drawn on the live feed
# after it fires, when the optional "Show Boxes" overlay is on. A
# still-active detection refreshes this naturally every cooldown
# period (see object_detector.DETECTION_COOLDOWN_SECONDS); once the
# subject actually leaves, the box just fades out of relevance within
# this window rather than lingering indefinitely.
DETECTION_BOX_TTL_SECONDS = 12.0

# Built lazily (first call) and cached -- a QPixmap can't be constructed
# before QApplication exists, so this can't just be a module-level
# constant. Every CameraTile shares the same icon instance rather than
# each one drawing its own; the glyph never changes, only its
# visibility does.
_motion_icon_cache = None


def _motion_icon_pixmap():
    """A small walking-person glyph, drawn procedurally with QPainter
    (head + angled torso/limb strokes suggesting movement) rather than
    bundling an external image asset -- consistent with how
    zone_editor.py's _DeleteGlyph draws its own glyph instead of
    loading one. One shared QPixmap, toggled visible/hidden per tile
    rather than redrawn per tile per tick."""
    global _motion_icon_cache
    if _motion_icon_cache is not None:
        return _motion_icon_cache

    size = MOTION_ICON_SIZE
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)

    pen = QPen(COLOR_MOTION_ACTIVE, 1.6)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(pen)
    painter.setBrush(QBrush(COLOR_MOTION_ACTIVE))

    # Head
    head_r = size * 0.13
    head_cx, head_cy = size * 0.62, size * 0.22
    painter.drawEllipse(QPointF(head_cx, head_cy), head_r, head_r)

    # Torso (leaning forward -- suggests motion rather than standing still)
    painter.drawLine(QPointF(head_cx, head_cy + head_r), QPointF(size * 0.45, size * 0.55))

    # Back leg (trailing, bent) and front leg (forward stride)
    painter.drawLine(QPointF(size * 0.45, size * 0.55), QPointF(size * 0.30, size * 0.85))
    painter.drawLine(QPointF(size * 0.45, size * 0.55), QPointF(size * 0.68, size * 0.80))

    # Back arm (trailing) and front arm (forward swing)
    painter.drawLine(QPointF(size * 0.50, size * 0.40), QPointF(size * 0.28, size * 0.50))
    painter.drawLine(QPointF(size * 0.50, size * 0.40), QPointF(size * 0.75, size * 0.42))

    painter.end()

    _motion_icon_cache = pixmap
    return pixmap


_detection_icon_cache = None


def _detection_icon_pixmap():
    """A small filled-circle glyph for the caption-bar detection badge
    -- deliberately a plain dot rather than reusing the motion walking
    icon's shape, so "an object was classified here" and "raw motion
    was seen here" read as distinct signals even when both could
    appear in the same caption bar at once. Colored to match the
    optional bounding-box overlay (COLOR_DETECTION_BOX_STROKE) so the
    two stay visually associated with the same feature."""
    global _detection_icon_cache
    if _detection_icon_cache is not None:
        return _detection_icon_cache

    size = MOTION_ICON_SIZE
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QBrush(COLOR_DETECTION_BOX_STROKE))
    r = size * 0.30
    painter.drawEllipse(QPointF(size / 2, size / 2), r, r)
    painter.end()

    _detection_icon_cache = pixmap
    return pixmap


def _status_text_and_color(status):
    if status == StreamStatus.CONNECTING:
        return "Connecting…", COLOR_STATUS_CONNECTING
    if status == StreamStatus.ERROR:
        return "Disconnected — retrying", COLOR_STATUS_ERROR
    return "", COLOR_TEXT_MUTED


class VideoLabel(QLabel):
    """QLabel subclass that also draws zone outlines on top of the
    displayed frame.

    Zones are stored normalized (0.0-1.0, fraction of frame width/
    height) -- see camera_store.py / zone_editor.py. To draw them in
    the right place we need to know exactly which rectangle of this
    widget the scaled video pixmap actually occupies (KeepAspectRatio
    scaling means the pixmap is usually letterboxed -- it doesn't fill
    the whole label), then map normalized coordinates into that rect.

    show_labels controls whether zone names are drawn next to the
    outline (used in single view) or suppressed (used on small grid
    tiles, where labels would just be clutter).
    """

    def __init__(self, show_labels=True, parent=None):
        super().__init__(parent)
        self.show_labels = show_labels
        self.zones_visible = True
        self.zones = []          # list of zone dicts: {"id","name","points"}
        self._frame_rect = None  # QRect of the actual pixmap within this label
        self._motion_zone_ids = frozenset()  # zone ids with motion right now
        # Phase 4 quality-of-life: optional bounding-box overlay.
        # Originally held at most one box (the single most recent
        # detection) because object_detector.py itself only ever
        # tracked one active detection per camera. Now that it tracks
        # multiple simultaneous instances (see object_detector.py's
        # "Multi-instance presence tracking"), this holds a list so
        # every currently active detection draws at once -- 2 people
        # and a car all present together show as 3 boxes, not 1. Off
        # by default (detection_box_visible) so boxes don't clutter
        # the feed constantly -- toggled per view via GridView/
        # SingleView's "Show Boxes" button, same pattern as
        # zones_visible above.
        self._detection_boxes = []  # list of (bbox_normalized, label)
        self.detection_box_visible = False

    def set_detection_boxes(self, boxes):
        """boxes: list of (bbox_normalized, label) tuples, one per
        currently active detection -- bbox_normalized is (x1, y1, x2,
        y2) fractions 0.0-1.0, same convention as zone points (see
        object_detector.DetectionEvent). Pass an empty list to clear
        all boxes. Always stored regardless of detection_box_visible
        so it's ready the instant the toggle is switched on, mirroring
        how set_zones() is always kept current independent of
        zones_visible."""
        if boxes != self._detection_boxes:
            self._detection_boxes = boxes
            self.update()

    def set_detection_box_visible(self, visible):
        if visible != self.detection_box_visible:
            self.detection_box_visible = visible
            self.update()

    def set_zones(self, zones):
        self.zones = zones or []
        self.update()

    def set_zones_visible(self, visible):
        self.zones_visible = visible
        self.update()

    def set_zone_motion(self, zone_ids_with_motion):
        """zone_ids_with_motion: iterable of zone ids currently flagged
        as having motion (per MotionResult.zones). Per-tick, no
        smoothing -- matches set_motion_active()'s raw-reflection
        behavior on CameraTile's name-bar icon. Outlines for zones in
        this set are drawn in the motion-highlight color instead of
        the resting zone-overlay color; everything else (labels,
        non-motion zones) is unaffected."""
        new_set = frozenset(zone_ids_with_motion or ())
        if new_set != self._motion_zone_ids:
            self._motion_zone_ids = new_set
            self.update()

    def set_frame_rect(self, rect):
        """Called by CameraTile.update_frame() right after scaling the
        pixmap, so we know where within the label the actual image
        pixels landed (letterboxing offsets included)."""
        self._frame_rect = rect
        self.update()

    def paintEvent(self, event):
        super().paintEvent(event)  # draws the pixmap (set via setPixmap)

        if self._frame_rect is None:
            return
        rect = self._frame_rect
        if rect.width() <= 0 or rect.height() <= 0:
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        if self.zones_visible and self.zones:
            resting_pen = QPen(COLOR_ZONE_OVERLAY_STROKE, 2)
            motion_pen = QPen(COLOR_ZONE_MOTION_STROKE, 2)
            painter.setPen(resting_pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)

            for zone in self.zones:
                points = zone.get("points", [])
                if len(points) < 3:
                    continue

                has_motion = zone.get("id") in self._motion_zone_ids
                poly = QPolygonF([
                    QPointF(
                        rect.x() + nx * rect.width(),
                        rect.y() + ny * rect.height(),
                    )
                    for nx, ny in points
                ])

                if has_motion:
                    # Filled highlight, not just a brighter outline -- a
                    # zone that's actively triggering should read clearly
                    # at a glance, the same way the resting overlay is
                    # deliberately outline-only/subtle.
                    painter.setPen(motion_pen)
                    painter.setBrush(QBrush(COLOR_ZONE_MOTION_FILL))
                    painter.drawPolygon(poly)
                    painter.setBrush(Qt.BrushStyle.NoBrush)
                    painter.setPen(resting_pen)
                else:
                    painter.drawPolygon(poly)

                if self.show_labels:
                    anchor = poly[0]
                    label = zone.get("name", "")
                    if label:
                        fm = painter.fontMetrics()
                        text_w = fm.horizontalAdvance(label) + 8
                        text_h = fm.height() + 4
                        bg_rect = QRectF(anchor.x(), anchor.y() - text_h - 2, text_w, text_h)
                        painter.setPen(Qt.PenStyle.NoPen)
                        painter.setBrush(QBrush(COLOR_ZONE_OVERLAY_LABEL_BG))
                        painter.drawRect(bg_rect)
                        painter.setPen(QPen(COLOR_ZONE_OVERLAY_LABEL_TEXT))
                        painter.drawText(
                            bg_rect.adjusted(4, 0, 0, 0),
                            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                            label,
                        )
                        painter.setBrush(Qt.BrushStyle.NoBrush)
                        painter.setPen(resting_pen)

        # Phase 4 quality-of-life, extended: bounding-box overlay for
        # EVERY currently active detection, not just the single most
        # recent one -- see set_detection_boxes's docstring. Each box
        # gets its own label chip; deliberately independent of
        # zones_visible/self.zones above -- a camera might have no
        # zones at all (whole-frame detection) and should still show
        # its boxes.
        if self.detection_box_visible and self._detection_boxes:
            box_pen = QPen(COLOR_DETECTION_BOX_STROKE, 2)
            for bbox_normalized, label in self._detection_boxes:
                nx1, ny1, nx2, ny2 = bbox_normalized
                box_rect = QRectF(
                    rect.x() + nx1 * rect.width(),
                    rect.y() + ny1 * rect.height(),
                    (nx2 - nx1) * rect.width(),
                    (ny2 - ny1) * rect.height(),
                )
                painter.setPen(box_pen)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRect(box_rect)

                if label:
                    fm = painter.fontMetrics()
                    text_w = fm.horizontalAdvance(label) + 8
                    text_h = fm.height() + 4
                    label_rect = QRectF(box_rect.x(), box_rect.y() - text_h - 2, text_w, text_h)
                    painter.setPen(Qt.PenStyle.NoPen)
                    painter.setBrush(QBrush(COLOR_DETECTION_BOX_LABEL_BG))
                    painter.drawRect(label_rect)
                    painter.setPen(QPen(COLOR_DETECTION_BOX_LABEL_TEXT))
                    painter.drawText(
                        label_rect.adjusted(4, 0, 0, 0),
                        Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                        label,
                    )

        painter.end()


class CameraTile(QFrame):
    """A single video tile: video area + caption bar showing the
    camera's assigned name and (when relevant) a connection status.

    Used both in the grid and in the single-camera view, so the name
    is always visible no matter which view you're in.
    """

    doubleClicked = pyqtSignal(dict)

    def __init__(self, camera, clickable=True, show_zone_labels=False, parent=None):
        super().__init__(parent)
        self.camera = camera
        self.clickable = clickable
        self._current_qimage = None  # keep a ref; avoids GC/flicker issues
        # Phase 6: the StreamWorker frame counter last actually drawn by
        # this tile. None means "never drawn yet" (distinct from any
        # real counter value, which starts at 0) so the very first
        # frame after tile creation or a camera switch always draws
        # regardless of what counter value it happens to carry.
        self._last_frame_counter = None

        self.setStyleSheet(f"""
            QFrame#tileRoot {{
                background-color: {COLOR_TILE_BG};
                border: 1px solid {COLOR_BORDER};
                border-radius: 4px;
            }}
        """)
        self.setObjectName("tileRoot")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.video_label = VideoLabel(show_labels=show_zone_labels)
        self.video_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.video_label.setStyleSheet(f"background-color: {COLOR_TILE_BG};")
        self.video_label.setMinimumSize(80, 60)
        self.video_label.set_zones(camera.get("zones", []))
        layout.addWidget(self.video_label, stretch=1)

        # ---- caption bar: camera name (always shown) + status -------
        caption = QFrame()
        caption.setStyleSheet(f"""
            background-color: {COLOR_CAPTION_BG};
            border-top: 1px solid {COLOR_CAPTION_BORDER};
        """)
        caption_layout = QHBoxLayout(caption)
        caption_layout.setContentsMargins(8, 4, 8, 4)

        self.name_label = QLabel(camera.get("name", ""))
        name_font = QFont()
        name_font.setPointSize(10)
        name_font.setBold(True)
        self.name_label.setFont(name_font)
        self.name_label.setStyleSheet(f"color: {COLOR_TEXT_PRIMARY};")
        caption_layout.addWidget(self.name_label)

        # Phase 3: motion indicator -- sits right of the name, before
        # the stretch/status text. Hidden by default; toggled visible
        # by set_motion_active() each poll tick (no hold/smoothing --
        # reflects the backend's raw per-tick motion state exactly).
        self.motion_icon_label = QLabel()
        self.motion_icon_label.setPixmap(_motion_icon_pixmap())
        self.motion_icon_label.setFixedSize(MOTION_ICON_SIZE, MOTION_ICON_SIZE)
        self.motion_icon_label.setVisible(False)
        caption_layout.addWidget(self.motion_icon_label)

        # Phase 4 quality-of-life: a compact "what was just classified
        # here" badge -- separate from the optional bounding-box
        # overlay (which draws on the video itself and is off by
        # default). This is always on regardless of that toggle, since
        # it's a small caption-bar indicator rather than something that
        # clutters the feed. Hidden when there's no recent detection;
        # driven by set_detection_badge() each poll tick, same TTL as
        # the bounding-box overlay (see DETECTION_BOX_TTL_SECONDS).
        self.detection_icon_label = QLabel()
        self.detection_icon_label.setPixmap(_detection_icon_pixmap())
        self.detection_icon_label.setFixedSize(MOTION_ICON_SIZE, MOTION_ICON_SIZE)
        self.detection_icon_label.setVisible(False)
        caption_layout.addWidget(self.detection_icon_label)

        self.detection_class_label = QLabel("")
        detection_font = QFont()
        detection_font.setPointSize(9)
        detection_font.setBold(True)
        self.detection_class_label.setFont(detection_font)
        self.detection_class_label.setStyleSheet(f"color: {COLOR_DETECTION_BOX_STROKE};")
        self.detection_class_label.setVisible(False)
        caption_layout.addWidget(self.detection_class_label)

        caption_layout.addStretch(1)

        # Phase 7 QoL: recording-error indicator -- deliberately silent
        # for every RecordingStatus except CODEC_ERROR, matching this
        # app's "only show status when something needs attention"
        # convention (see _status_text_and_color). NO_FRAME isn't shown
        # here since the stream status text below already covers "this
        # camera hasn't connected yet"; RECORDING is the normal, quiet
        # state. Driven by set_recording_status() each poll tick.
        self.recording_status_label = QLabel("")
        recording_status_font = QFont()
        recording_status_font.setPointSize(9)
        self.recording_status_label.setFont(recording_status_font)
        self.recording_status_label.setStyleSheet(f"color: {COLOR_STATUS_ERROR};")
        caption_layout.addWidget(self.recording_status_label)

        self.status_label = QLabel("")
        status_font = QFont()
        status_font.setPointSize(9)
        self.status_label.setFont(status_font)
        caption_layout.addWidget(self.status_label)

        layout.addWidget(caption, stretch=0)

        if self.clickable:
            self.setCursor(Qt.CursorShape.PointingHandCursor)

    def mouseDoubleClickEvent(self, event):
        if self.clickable:
            self.doubleClicked.emit(self.camera)
        super().mouseDoubleClickEvent(event)

    def set_motion_active(self, active):
        """Toggle the caption-bar motion icon. Called once per poll
        tick from GridView/SingleView with the camera's current
        whole-frame motion state -- no internal smoothing/hold here,
        by design (raw per-tick reflection, can flicker at the
        backend's ~5fps detection cadence)."""
        self.motion_icon_label.setVisible(bool(active))

    def set_detection_badge(self, class_name):
        """Toggle the caption-bar detection badge (icon + class name).
        Pass a class name string to show it, or None/"" to hide it.
        Always driven regardless of the "Show Boxes" toggle -- this is
        a compact always-on indicator, not the optional on-video
        overlay, so it stays useful even for people who never turn
        boxes on."""
        active = bool(class_name)
        self.detection_icon_label.setVisible(active)
        self.detection_class_label.setVisible(active)
        self.detection_class_label.setText(class_name.capitalize() if active else "")

    def set_recording_status(self, status):
        """Toggle the caption-bar recording-error text. See the label's
        construction comment for why every state except CODEC_ERROR
        renders as nothing."""
        if status == RecordingStatus.CODEC_ERROR:
            self.recording_status_label.setText("⚠ Recording error")
        else:
            self.recording_status_label.setText("")

    def update_frame(self, frame_bgr, frame_counter=None):
        """frame_bgr: numpy array (OpenCV BGR) or None if no frame yet.

        frame_counter: StreamWorker's monotonic frame counter for this
        camera, if the caller has it (GridView/SingleView do; anything
        calling this directly without one -- there currently isn't --
        just always redraws, since None never equals another value in
        the check below). Phase 6: skips the cv2.cvtColor + QImage +
        QPixmap.scaled work entirely when the poll tick's frame is the
        same one already on screen -- RTSP cameras frequently decode
        slower than the ~30fps GUI poll rate, so a large fraction of
        poll ticks would otherwise be re-doing identical work on pixels
        that haven't changed.
        """
        if frame_bgr is None:
            return
        if frame_counter is not None and frame_counter == self._last_frame_counter:
            return  # nothing new since the last time we actually drew
        self._last_frame_counter = frame_counter

        target_w = max(self.video_label.width(), 1)
        target_h = max(self.video_label.height(), 1)
        if target_w <= 1 or target_h <= 1:
            return

        frame_rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        h, w, ch = frame_rgb.shape
        bytes_per_line = ch * w
        qimg = QImage(frame_rgb.data, w, h, bytes_per_line, QImage.Format.Format_RGB888)

        # Keep a reference -- QImage doesn't own frame_rgb's buffer, and
        # without this the underlying numpy data could be freed/reused
        # by the next OpenCV read before Qt finishes painting it.
        self._current_qimage = qimg

        pixmap = QPixmap.fromImage(qimg)
        pixmap = pixmap.scaled(
            target_w,
            target_h,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.video_label.setPixmap(pixmap)

        # KeepAspectRatio means the pixmap is usually letterboxed inside
        # the label (centered, with empty bars on two sides). Compute
        # exactly where it landed so zone overlays -- drawn in
        # VideoLabel.paintEvent -- line up with the actual image pixels
        # instead of the label's full bounds.
        offset_x = (target_w - pixmap.width()) // 2
        offset_y = (target_h - pixmap.height()) // 2
        self.video_label.set_frame_rect(QRect(offset_x, offset_y, pixmap.width(), pixmap.height()))

    def set_zones(self, zones):
        self.video_label.set_zones(zones)

    def set_zones_visible(self, visible):
        self.video_label.set_zones_visible(visible)

    def set_zone_motion(self, zone_ids_with_motion):
        self.video_label.set_zone_motion(zone_ids_with_motion)

    def set_detection_boxes(self, boxes):
        self.video_label.set_detection_boxes(boxes)

    def set_detection_box_visible(self, visible):
        self.video_label.set_detection_box_visible(visible)

    def set_status(self, status):
        text, color = _status_text_and_color(status)
        self.status_label.setText(text)
        self.status_label.setStyleSheet(f"color: {color};")

    def set_camera(self, camera):
        """Update which camera this tile displays (used by single view
        when switching cameras without rebuilding the whole widget)."""
        self.camera = camera
        self.name_label.setText(camera.get("name", ""))
        self.video_label.set_zones(camera.get("zones", []))
        # Clear stale motion/detection state from whichever camera this
        # tile was previously showing -- otherwise a leftover "motion"
        # icon/zone highlight/detection box from the old camera could
        # flash briefly before the next poll tick corrects it.
        self.set_motion_active(False)
        self.video_label.set_zone_motion(())
        self.video_label.set_detection_boxes([])
        self.set_detection_badge(None)
        self.set_recording_status(RecordingStatus.NO_FRAME)
        # New camera has its own independent frame counter sequence --
        # reset so the next frame (whatever counter value it carries)
        # is always drawn instead of possibly matching a stale value
        # left over from the previous camera.
        self._last_frame_counter = None


class GridView(QWidget):
    """All active cameras arranged in a roughly-square grid."""

    cameraActivated = pyqtSignal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.tiles = {}  # cam_id -> CameraTile
        self.zones_visible = True
        self.boxes_visible = False  # off by default -- opt-in, see design discussion

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        header = QFrame()
        header.setStyleSheet(f"background-color: {COLOR_PANEL_BG}; border-bottom: 1px solid {COLOR_BORDER};")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(10, 4, 10, 4)
        header_layout.addStretch(1)

        self.boxes_toggle_btn = QPushButton("Show Boxes")
        self.boxes_toggle_btn.setCheckable(True)
        self.boxes_toggle_btn.setChecked(False)
        self.boxes_toggle_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.boxes_toggle_btn.setStyleSheet(_button_style())
        self.boxes_toggle_btn.toggled.connect(self._on_boxes_toggled)
        header_layout.addWidget(self.boxes_toggle_btn)

        self.zones_toggle_btn = QPushButton("Hide Zones")
        self.zones_toggle_btn.setCheckable(True)
        self.zones_toggle_btn.setChecked(True)
        self.zones_toggle_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.zones_toggle_btn.setStyleSheet(_button_style())
        self.zones_toggle_btn.toggled.connect(self._on_zones_toggled)
        header_layout.addWidget(self.zones_toggle_btn)

        outer.addWidget(header, stretch=0)

        grid_container = QWidget()
        self.layout_ = QGridLayout(grid_container)
        self.layout_.setContentsMargins(6, 6, 6, 6)
        self.layout_.setSpacing(6)
        outer.addWidget(grid_container, stretch=1)

        self.empty_label = QLabel("No cameras yet.\nOpen Settings to add one.")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font-size: 14px;")

    def _on_zones_toggled(self, checked):
        self.zones_visible = checked
        self.zones_toggle_btn.setText("Hide Zones" if checked else "Show Zones")
        for tile in self.tiles.values():
            tile.set_zones_visible(checked)

    def _on_boxes_toggled(self, checked):
        self.boxes_visible = checked
        self.boxes_toggle_btn.setText("Hide Boxes" if checked else "Show Boxes")
        for tile in self.tiles.values():
            tile.set_detection_box_visible(checked)

    def rebuild(self, cameras):
        # Clear existing widgets/layout entries.
        while self.layout_.count():
            item = self.layout_.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
        self.tiles = {}

        if not cameras:
            self.layout_.addWidget(self.empty_label, 0, 0)
            return

        rows, cols = grid_dimensions(len(cameras))
        for r in range(rows):
            self.layout_.setRowStretch(r, 1)
        for c in range(cols):
            self.layout_.setColumnStretch(c, 1)

        positions = tile_positions(len(cameras))
        for camera, (r, c) in zip(cameras, positions):
            # show_zone_labels=False: grid tiles are small, outlines
            # only -- labels would just be clutter at thumbnail size.
            tile = CameraTile(camera, clickable=True, show_zone_labels=False)
            tile.set_zones_visible(self.zones_visible)
            tile.set_detection_box_visible(self.boxes_visible)
            tile.doubleClicked.connect(self.cameraActivated.emit)
            self.layout_.addWidget(tile, r, c)
            self.tiles[camera["id"]] = tile

    def update_frames(self, stream_manager, motion_manager=None, detection_manager=None, recording_manager=None):
        for cam_id, tile in self.tiles.items():
            frame = stream_manager.get_frame(cam_id)
            frame_counter = stream_manager.get_frame_counter(cam_id)
            tile.update_frame(frame, frame_counter=frame_counter)
            status, _err = stream_manager.get_status(cam_id)
            tile.set_status(status)

            if recording_manager is not None:
                tile.set_recording_status(recording_manager.get_status(cam_id))

            # Grid tiles only show the name-bar motion icon (whole-frame
            # bool) -- no per-zone outline highlighting here, consistent
            # with show_zone_labels=False above: small thumbnails stay
            # legible, per-zone detail belongs in single view only.
            if motion_manager is not None:
                result = motion_manager.get_result(cam_id)
                tile.set_motion_active(result.motion)

            # Phase 4 quality-of-life, extended for multi-instance
            # tracking: the box overlay now draws every currently
            # active detection at once (get_active_detections), not
            # just the single most recent one -- see VideoLabel.
            # set_detection_boxes's docstring. No TTL math needed here
            # anymore; object_detector.py's presence slots already
            # self-expire (ABSENCE_TIMEOUT_SECONDS) and
            # get_active_detections only ever returns what's still
            # active right now. The caption-bar badge is a separate,
            # smaller indicator ("something was recently seen") and
            # deliberately still shows only the single latest
            # detection -- see main.py's known-limitation note.
            if detection_manager is not None:
                event = detection_manager.get_latest_event(cam_id)
                badge_active = event is not None and (time.time() - event.timestamp) <= DETECTION_BOX_TTL_SECONDS
                tile.set_detection_badge(event.class_name if badge_active else None)

                active_detections = detection_manager.get_active_detections(cam_id)
                boxes = [
                    (bbox, f"{class_name.capitalize()} {confidence * 100:.0f}%")
                    for class_name, confidence, bbox in active_detections
                ]
                tile.set_detection_boxes(boxes)


class SingleView(QWidget):
    """Full-size view of one camera, with a button to go back to the
    grid and a "Define Zones" toggle that swaps the plain video tile
    out for the interactive ZoneEditorView.
    """

    backRequested = pyqtSignal()

    def __init__(self, app, parent=None):
        super().__init__(parent)
        self.app = app  # gives access to app.store for zone persistence
        self.camera = None
        self.tile = None
        self.zone_editing = False
        self.zones_visible = True
        self.boxes_visible = False  # off by default -- opt-in, see design discussion

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        top_bar = QFrame()
        top_bar.setStyleSheet(f"background-color: {COLOR_PANEL_BG}; border-bottom: 1px solid {COLOR_BORDER};")
        top_bar_layout = QHBoxLayout(top_bar)
        top_bar_layout.setContentsMargins(8, 6, 8, 6)

        back_btn = QPushButton("←  Back to grid")
        back_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        back_btn.setStyleSheet(_button_style())
        back_btn.clicked.connect(self.backRequested.emit)
        top_bar_layout.addWidget(back_btn)
        top_bar_layout.addStretch(1)

        self.zone_hint_label = QLabel(
            "Click to place points, click the first point (or double-click) to close. Esc cancels."
        )
        self.zone_hint_label.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font-size: 11px;")
        self.zone_hint_label.setVisible(False)
        top_bar_layout.addWidget(self.zone_hint_label)
        top_bar_layout.addStretch(1)

        self.boxes_visible_btn = QPushButton("Show Boxes")
        self.boxes_visible_btn.setCheckable(True)
        self.boxes_visible_btn.setChecked(False)
        self.boxes_visible_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.boxes_visible_btn.setStyleSheet(_button_style())
        self.boxes_visible_btn.toggled.connect(self._on_boxes_visible_toggled)
        top_bar_layout.addWidget(self.boxes_visible_btn)

        self.zones_visible_btn = QPushButton("Hide Zones")
        self.zones_visible_btn.setCheckable(True)
        self.zones_visible_btn.setChecked(True)
        self.zones_visible_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.zones_visible_btn.setStyleSheet(_button_style())
        self.zones_visible_btn.toggled.connect(self._on_zones_visible_toggled)
        top_bar_layout.addWidget(self.zones_visible_btn)

        self.zones_btn = QPushButton("Define Zones")
        self.zones_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.zones_btn.setCheckable(True)
        self.zones_btn.setStyleSheet(_button_style())
        self.zones_btn.toggled.connect(self._on_zones_toggled)
        top_bar_layout.addWidget(self.zones_btn)

        outer.addWidget(top_bar, stretch=0)

        self.body = QVBoxLayout()
        self.body.setContentsMargins(6, 6, 6, 6)
        body_container = QWidget()
        body_container.setLayout(self.body)
        outer.addWidget(body_container, stretch=1)

        # Built once, reused across camera switches (cheaper than
        # rebuilding a QGraphicsScene every time), shown only while
        # zone_editing is True.
        self.zone_editor = ZoneEditorView()
        self.zone_editor.zoneFinalized.connect(self._on_zone_finalized)
        self.zone_editor.zoneDeleteRequested.connect(self._on_zone_delete_requested)
        self.zone_editor.zoneModified.connect(self._on_zone_modified)

    def set_camera(self, camera):
        self.camera = camera
        self._rebuild_body()

    def _rebuild_body(self):
        # Clear previous widget from the body layout without destroying
        # self.zone_editor itself, since we reuse it across cameras.
        while self.body.count():
            item = self.body.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)

        if self.zone_editing:
            self.zone_editor.set_camera(self.camera)
            self.zone_editor.set_edit_mode(True)
            self.body.addWidget(self.zone_editor)
        else:
            # show_zone_labels=True: single view has room to show zone
            # names next to their outlines, unlike the small grid tiles.
            self.tile = CameraTile(self.camera, clickable=False, show_zone_labels=True)
            self.tile.set_zones_visible(self.zones_visible)
            self.tile.set_detection_box_visible(self.boxes_visible)
            self.body.addWidget(self.tile)

    def update_frame(self, stream_manager, motion_manager=None, detection_manager=None, recording_manager=None):
        if self.camera is None:
            return
        frame = stream_manager.get_frame(self.camera["id"])
        status, _err = stream_manager.get_status(self.camera["id"])

        if self.zone_editing:
            self.zone_editor.update_frame(frame)
        elif self.tile is not None:
            frame_counter = stream_manager.get_frame_counter(self.camera["id"])
            self.tile.update_frame(frame, frame_counter=frame_counter)
            self.tile.set_status(status)

            if recording_manager is not None:
                self.tile.set_recording_status(recording_manager.get_status(self.camera["id"]))

            # Single view gets the full Phase 3 treatment: the same
            # name-bar icon grid tiles get, plus per-zone outline
            # highlighting (which zone(s) specifically have motion
            # right now) -- there's room here that a small grid
            # thumbnail doesn't have, and it's the more useful view
            # when you're actually looking at one camera.
            if motion_manager is not None:
                result = motion_manager.get_result(self.camera["id"])
                self.tile.set_motion_active(result.motion)
                self.tile.set_zone_motion(
                    zone_id for zone_id, has_motion in result.zones.items() if has_motion
                )

            # Phase 4 quality-of-life, extended for multi-instance
            # tracking: same multi-box overlay + single-latest badge
            # split as GridView -- see that method's comment for the
            # full reasoning.
            if detection_manager is not None:
                event = detection_manager.get_latest_event(self.camera["id"])
                badge_active = event is not None and (time.time() - event.timestamp) <= DETECTION_BOX_TTL_SECONDS
                self.tile.set_detection_badge(event.class_name if badge_active else None)

                active_detections = detection_manager.get_active_detections(self.camera["id"])
                boxes = [
                    (bbox, f"{class_name.capitalize()} {confidence * 100:.0f}%")
                    for class_name, confidence, bbox in active_detections
                ]
                self.tile.set_detection_boxes(boxes)

    # ----- zone mode toggle ---------------------------------------------

    def _on_zones_toggled(self, checked):
        self.zone_editing = checked
        self.zone_hint_label.setVisible(checked)
        self.zones_btn.setText("Done" if checked else "Define Zones")
        # The "Show/Hide Zones" toggle is about the passive overlay --
        # editing mode always shows zones (you need to see them to
        # edit them), so disable that button while editing rather than
        # letting it fight with edit mode.
        self.zones_visible_btn.setEnabled(not checked)
        self._rebuild_body()

    def _on_zones_visible_toggled(self, checked):
        self.zones_visible = checked
        self.zones_visible_btn.setText("Hide Zones" if checked else "Show Zones")
        if not self.zone_editing and self.tile is not None:
            self.tile.set_zones_visible(checked)

    def _on_boxes_visible_toggled(self, checked):
        self.boxes_visible = checked
        self.boxes_visible_btn.setText("Hide Boxes" if checked else "Show Boxes")
        if not self.zone_editing and self.tile is not None:
            self.tile.set_detection_box_visible(checked)

    # ----- zone editor signal handlers -----------------------------------

    def _on_zone_finalized(self, normalized_points):
        if self.camera is None:
            return

        if not self.app.store.can_add_zone(self.camera["id"]):
            QMessageBox.information(
                self,
                "Zone limit reached",
                "You've reached the maximum number of zones for this camera.",
            )
            return

        name, ok = QInputDialog.getText(self, "Name this zone", "Zone name:")
        name = (name or "").strip()
        if not ok or not name:
            return  # user cancelled or left it blank -- drop the draft silently

        try:
            zone = self.app.store.add_zone(self.camera["id"], name, normalized_points)
        except ValueError as exc:
            QMessageBox.critical(self, "Couldn't add zone", str(exc))
            return

        self.zone_editor.add_zone_to_scene(zone["id"], zone["name"], zone["points"])
        self.app.notify_zones_changed(self.camera["id"])

    def _on_zone_delete_requested(self, zone_id):
        if self.camera is None:
            return
        reply = QMessageBox.question(
            self,
            "Remove zone",
            "Remove this zone?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self.app.store.remove_zone(self.camera["id"], zone_id)
        self.zone_editor.remove_zone_from_scene(zone_id)
        self.app.notify_zones_changed(self.camera["id"])

    def _on_zone_modified(self, zone_id, normalized_points):
        if self.camera is None:
            return
        try:
            self.app.store.update_zone(self.camera["id"], zone_id, points=normalized_points)
        except ValueError as exc:
            QMessageBox.critical(self, "Couldn't update zone", str(exc))
        else:
            self.app.notify_zones_changed(self.camera["id"])


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


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("CCTV Viewer")
        self.resize(1100, 750)
        self.setStyleSheet(f"background-color: {COLOR_BG};")

        self.store = CameraStore()
        self.streams = StreamManager()
        self.motion = MotionManager(self.streams, self.store)
        self.detection = ObjectDetectionManager(self.streams, self.motion, self.store)
        self._detection_events_seen = 0

        # Phase 7: local recording + SQLite event/segment index. Always-
        # on for every camera (no per-camera switch, no Settings UI --
        # see Phase 7 design discussion). event_store is the single
        # sqlite3 access point; recording writes segments, event_logger
        # writes motion lifecycle rows, and object-detection rows are
        # logged directly below in _poll (see event_logger.py's
        # docstring for why that one path doesn't get its own worker).
        # Constructed before AlertManager below, since alerts now write
        # into event_store too and need both handed to them.
        self.event_store = EventStore()
        self.recording = RecordingManager(self.streams, self.event_store)
        self.event_logger = EventLoggerManager(self.motion, self.recording, self.event_store)

        # Phase 5: alerting. Deliberately NOT wired through
        # notify_zones_changed's hub pattern -- see alert_manager.py's
        # module docstring for why alerts need their own independent
        # background lifecycle rather than a push-to-active-view hook.
        # Phase 7 addition: event_store/recording_manager are passed
        # through so alert START/END edges also land in the persistent
        # events table (see alert_manager.py's "Phase 7 addition"
        # docstring section) -- the log-file channel is unaffected,
        # this is a second, parallel write path.
        self.alerts = AlertManager(
            self.motion, self.detection, self.store,
            event_store=self.event_store, recording_manager=self.recording,
        )

        central = QWidget()
        self.setCentralWidget(central)
        # Root layout is horizontal: a left column (toolbar + the
        # grid/single/settings stack) plus the detection panel as a
        # sibling on the right -- NOT inside the stack, so it stays
        # visible/collapsible no matter which page the stack is
        # currently showing (grid, single camera, or settings).
        root_layout = QHBoxLayout(central)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        left_container = QWidget()
        outer = QVBoxLayout(left_container)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # ---- toolbar -------------------------------------------------
        toolbar = QFrame()
        toolbar.setStyleSheet(f"background-color: {COLOR_PANEL_BG}; border-bottom: 1px solid {COLOR_BORDER};")
        toolbar_layout = QHBoxLayout(toolbar)
        toolbar_layout.setContentsMargins(10, 8, 10, 8)

        title_label = QLabel("CCTV Viewer")
        title_font = QFont()
        title_font.setPointSize(12)
        title_font.setBold(True)
        title_label.setFont(title_font)
        title_label.setStyleSheet(f"color: {COLOR_TEXT_PRIMARY};")
        toolbar_layout.addWidget(title_label)
        toolbar_layout.addStretch(1)

        # Phase 4: toggles the right-side detection panel on/off from
        # anywhere -- same interaction pattern as the Settings button
        # below, but a visibility toggle rather than a page swap.
        self.detections_btn = QPushButton("Hide Detections")
        self.detections_btn.setCheckable(True)
        self.detections_btn.setChecked(True)
        self.detections_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.detections_btn.setStyleSheet(_button_style())
        self.detections_btn.toggled.connect(self._on_detections_toggled)
        toolbar_layout.addWidget(self.detections_btn)

        settings_btn = QPushButton("Settings")
        settings_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        settings_btn.setStyleSheet(_button_style())
        settings_btn.clicked.connect(self.toggle_settings_view)
        self.settings_btn = settings_btn
        toolbar_layout.addWidget(settings_btn)

        outer.addWidget(toolbar, stretch=0)

        # ---- stacked grid/single/settings view -------------------------
        self.stack = QStackedWidget()
        self.grid_view = GridView()
        self.single_view = SingleView(self)
        self.settings_view = SettingsPanel(self)
        self.grid_view.cameraActivated.connect(self.show_single_view)
        self.single_view.backRequested.connect(self.show_grid_view)

        self.stack.addWidget(self.grid_view)
        self.stack.addWidget(self.single_view)
        self.stack.addWidget(self.settings_view)
        outer.addWidget(self.stack, stretch=1)

        root_layout.addWidget(left_container, stretch=1)

        self._pre_settings_widget = None  # remembers where to return to

        # ---- Phase 4: persistent right-side detections panel -----------
        self.detection_panel = DetectionSidePanel(self)
        self.detection_panel.hideRequested.connect(lambda: self.detections_btn.setChecked(False))
        self.detection_panel.eventActivated.connect(self._on_detection_event_activated)
        root_layout.addWidget(self.detection_panel, stretch=0)

        self.show_grid_view()
        self._restart_all_streams()
        self._update_detection_panel_width()
        self.detection_panel.set_cameras(self.store.list_cameras())

        self.poll_timer = QTimer(self)
        self.poll_timer.timeout.connect(self._poll)
        self.poll_timer.start(POLL_INTERVAL_MS)

    # ----- detection panel: visibility + width capping -----------------

    def _on_detections_toggled(self, checked):
        self.detection_panel.setVisible(checked)
        self.detections_btn.setText("Hide Detections" if checked else "Show Detections")

    def _update_detection_panel_width(self):
        """Caps the detection panel at 15% of the window's current
        width (with a small floor so it stays usable on narrow
        windows). setFixedWidth rather than setMaximumWidth so the
        panel reliably occupies that width instead of shrinking to its
        content's sizeHint."""
        width = max(DetectionSidePanel.MIN_WIDTH, int(self.width() * DetectionSidePanel.WIDTH_FRACTION))
        self.detection_panel.setFixedWidth(width)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_detection_panel_width()

    # ----- view switching -------------------------------------------

    def show_grid_view(self):
        self.single_view.zones_btn.setChecked(False)
        self.grid_view.rebuild(self.store.list_cameras())
        self.stack.setCurrentWidget(self.grid_view)
        self.settings_btn.setText("Settings")

    def show_single_view(self, camera):
        self.single_view.set_camera(camera)
        self.stack.setCurrentWidget(self.single_view)
        self.settings_btn.setText("Settings")

    def _on_detection_event_activated(self, cam_id):
        """Click-to-jump: a detection log entry was clicked. Works from
        anywhere -- grid, single view, or settings -- since it's just a
        stack swap, same as any other camera activation."""
        camera = self.store.get_camera(cam_id)
        if camera is None:
            return  # camera was removed since the event was logged
        self.show_single_view(camera)

    def toggle_settings_view(self):
        if self.stack.currentWidget() is self.settings_view:
            # Leaving settings -- go back wherever we came from.
            if self._pre_settings_widget is self.single_view and self.single_view.camera is not None:
                self.show_single_view(self.single_view.camera)
            else:
                self.show_grid_view()
            return

        self._pre_settings_widget = self.stack.currentWidget()
        self.settings_view.refresh_current_section()
        self.stack.setCurrentWidget(self.settings_view)
        self.settings_btn.setText("← Back")

    # ----- camera CRUD (delegates to store, manages stream lifecycle) --

    def add_camera(self, name, url, cam_type):
        cam = self.store.add_camera(name, url, cam_type)
        self.streams.start_stream(cam["id"], cam["url"])
        self.motion.start_detection(cam["id"])
        self.detection.start_detection(cam["id"])
        self.alerts.start_alerts(cam["id"])
        self.recording.start_recording(cam["id"])
        self.event_logger.start_logging(cam["id"])
        self.detection_panel.set_cameras(self.store.list_cameras())
        return cam

    def update_camera(self, cam_id, name=None, url=None, cam_type=None):
        old_cam = self.store.get_camera(cam_id)
        old_url = old_cam["url"] if old_cam else None

        cam = self.store.update_camera(cam_id, name=name, url=url, cam_type=cam_type)

        # If the URL changed, the stream must be restarted against the
        # new address. If only the name/type changed, leave the running
        # stream alone to avoid an unnecessary reconnect glitch. Motion
        # detection's background model is keyed to what the camera is
        # actually pointed at, so it needs the same restart-on-URL-
        # change treatment -- otherwise it would keep comparing new
        # frames from a different physical scene against a background
        # model built from the old one. Object detection doesn't hold
        # any comparable per-frame state (each inference call is
        # independent), but its per-region cooldown/first-seen state
        # is scoped to what the camera *used* to be pointed at, so it
        # gets the same restart treatment for consistency.
        if url is not None and url != old_url:
            self.streams.start_stream(cam_id, cam["url"])
            self.motion.start_detection(cam_id)
            self.detection.start_detection(cam_id)
            # Same restart-on-URL-change treatment as motion/detection:
            # any in-progress alert duration was tracking the old
            # physical scene, so it gets closed out and re-armed fresh
            # against the new one rather than spanning the switch.
            self.alerts.start_alerts(cam_id)
            # Phase 7: a segment already mid-recording, and any open
            # motion_start with no motion_end yet, were both tracking
            # the old physical scene too -- close both out and start
            # fresh against the new one rather than letting either span
            # the switch.
            self.recording.start_recording(cam_id)
            self.event_logger.start_logging(cam_id)

        # If the single view is currently showing this camera, update
        # its displayed name immediately (e.g. after an edit).
        if self.single_view.camera and self.single_view.camera["id"] == cam_id:
            self.single_view.set_camera(cam)

        # Name may have changed -- keep the detection panel's camera
        # filter dropdown showing the current name.
        self.detection_panel.set_cameras(self.store.list_cameras())

        return cam

    def remove_camera(self, cam_id):
        self.streams.stop_stream(cam_id)
        self.motion.stop_detection(cam_id)
        self.detection.stop_detection(cam_id)
        self.alerts.stop_alerts(cam_id)
        self.event_logger.stop_logging(cam_id)
        self.recording.stop_recording(cam_id)
        self.store.remove_camera(cam_id)
        if self.single_view.camera and self.single_view.camera["id"] == cam_id:
            self.single_view.camera = None
            if self.stack.currentWidget() is self.single_view:
                self.show_grid_view()
        self.detection_panel.set_cameras(self.store.list_cameras())

    # ----- zone change notifications (live-sync across views) --------

    def notify_zones_changed(self, cam_id):
        """Called whenever a camera's zones are added/edited/removed,
        from anywhere (SingleView's zone editor, or the Zones settings
        section). Refreshes whichever live views currently hold stale
        cached zone lists for that camera, without a full rebuild.

        Both grid tiles and the single-view tile read straight from
        the camera dict (which the store already mutated in place), so
        this just needs to tell the relevant VideoLabel to re-pull and
        repaint -- no data re-fetching needed here.

        object_detector.py is deliberately NOT wired into this hub --
        unlike MotionWorker's mask cache, ObjectDetectionWorker doesn't
        cache anything zone-shaped; it reads camera["zones"] fresh from
        the store on every tick, so a zone edit just takes effect on
        the next tick with no invalidation call needed.

        alert_manager.py is the same story, one level further removed:
        AlertWorker never reads zones directly at all -- it only reads
        MotionManager's already-computed result and
        ObjectDetectionManager's already-computed latest event, both of
        which are themselves kept current by the mechanisms above. No
        alert-specific invalidation needed here either.

        event_logger.py is the same story as alert_manager.py --
        EventLoggerWorker only reads MotionManager's already-computed
        result, never zones directly. recording_manager.py doesn't
        read zones at all. Neither needs an invalidation call here.
        """
        cam = self.store.get_camera(cam_id)
        if cam is None:
            return

        tile = self.grid_view.tiles.get(cam_id)
        if tile is not None:
            tile.set_zones(cam.get("zones", []))

        if (
            self.single_view.camera
            and self.single_view.camera["id"] == cam_id
            and not self.single_view.zone_editing
            and self.single_view.tile is not None
        ):
            self.single_view.tile.set_zones(cam.get("zones", []))

        # If the Zones settings section is open on this same camera,
        # refresh its zone list + canvas too -- e.g. a zone drawn or
        # edited live in SingleView should show up there immediately
        # without needing to re-pick the camera in the dropdown.
        zones_panel = self.settings_view.get_zones_panel()
        if zones_panel is not None and zones_panel.current_camera_id == cam_id:
            zones_panel.sync_external_change()

        # Phase 3: this camera's zone-restricted motion masks are now
        # stale (a zone was added/edited/removed) -- invalidate so the
        # next processed frame rebuilds them, same single-hub pattern
        # as the view-sync calls above.
        self.motion.notify_zones_changed(cam_id)

    def _restart_all_streams(self):
        self.streams.stop_all()
        self.motion.stop_all()
        self.detection.stop_all()
        self.alerts.stop_all()
        self.event_logger.stop_all()
        self.recording.stop_all()
        for cam in self.store.list_cameras():
            self.streams.start_stream(cam["id"], cam["url"])
            self.motion.start_detection(cam["id"])
            self.detection.start_detection(cam["id"])
            self.alerts.start_alerts(cam["id"])
            self.recording.start_recording(cam["id"])
            self.event_logger.start_logging(cam["id"])

    # ----- frame polling loop ----------------------------------------

    def _poll(self):
        current = self.stack.currentWidget()
        if current is self.grid_view:
            self.grid_view.update_frames(self.streams, self.motion, self.detection, self.recording)
        elif current is self.single_view:
            self.single_view.update_frame(self.streams, self.motion, self.detection, self.recording)

        # Phase 4: pull any new detection events into the side panel, on
        # the same poll tick as everything else. Cheap even at ~30fps --
        # get_new_events() just copies out of a small bounded deque
        # under a short-held lock, and is a no-op (empty list) on most
        # ticks since inference itself is gated way down in
        # object_detector.py. Still updated even while the panel is
        # hidden, so the log is caught up whenever it's shown again.
        new_events, self._detection_events_seen = self.detection.get_new_events(
            self._detection_events_seen
        )
        if new_events:
            self.detection_panel.add_events(new_events)
            # Phase 7: same new_events list also feeds the permanent
            # SQLite record -- see _log_detection_events below for why
            # only fresh detections (not "still here" confirmations)
            # get a row.
            self._log_detection_events(new_events)

    def _log_detection_events(self, events):
        """Writes each fresh object-detection event into event_store,
        tagged with whichever segment is currently being recorded for
        that camera (None if recording hasn't opened one yet -- e.g.
        right at startup). Only fresh detections (is_new) are logged,
        not "still here" confirmations -- those exist to keep the live
        side panel informative during an ongoing presence, but would
        just duplicate the same underlying event over and over in the
        permanent record; the first "detected" row already establishes
        when the presence began.

        Also persists the event's thumbnail (already captured and
        JPEG-encoded by object_detector.py -- see DetectionEvent.
        thumbnail) via save_event_thumbnail, keyed by the row id
        add_event returns. This was a real gap until now: the
        DetectionSidePanel already showed these thumbnails, but only
        from ObjectDetectionManager's bounded in-memory log -- nothing
        persisted them, so the Events section's history had no way to
        show what actually triggered each detection."""
        for event in events:
            if not event.is_new:
                continue
            segment_id = self.recording.get_current_segment_id(event.camera_id)
            event_id = self.event_store.add_event(
                camera_id=event.camera_id,
                detected_at_iso=datetime.fromtimestamp(event.timestamp).isoformat(timespec="seconds"),
                detection_class=event.class_name,
                zone_id=event.zone_id,
                confidence=event.confidence,
                segment_id=segment_id,
            )
            if event.thumbnail:
                save_event_thumbnail(event_id, event.thumbnail)
            # QoL fix: without this, the clip containing this exact
            # detection isn't playable until its segment closes -- up
            # to 30 minutes away (MP4's moov atom only finalizes on
            # release()). This schedules an early close instead, so
            # it's reviewable in well under a minute. Rate-limited
            # inside RecordingWorker itself (see
            # MIN_FORCED_ROLLOVER_GAP_SECONDS) so a burst of detections
            # doesn't fragment recording into a pile of tiny files.
            self.recording.request_early_rollover(event.camera_id)

    def closeEvent(self, event):
        self.poll_timer.stop()
        # event_logger and alerts both close out a still-open lifecycle
        # on stop() and tag that closing row with
        # recording.get_current_segment_id() -- both need to run
        # BEFORE recording.stop_all() closes the segment out from
        # under them, or that lookup returns None and the last
        # motion/alert row active at shutdown loses its link to the
        # clip that was actually recording during it.
        self.event_logger.stop_all()
        self.alerts.stop_all()
        self.recording.stop_all()
        self.detection.stop_all()
        self.motion.stop_all()
        self.streams.stop_all()
        super().closeEvent(event)


def _apply_dark_palette(app):
    """Belt-and-suspenders dark theme: sets the app-wide QPalette in
    addition to per-widget stylesheets, so native dialogs (file pickers,
    message boxes) and any unstyled widget still look consistent rather
    than flashing white."""
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(COLOR_BG))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(COLOR_TEXT_PRIMARY))
    palette.setColor(QPalette.ColorRole.Base, QColor(COLOR_CAPTION_BG))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(COLOR_PANEL_BG))
    palette.setColor(QPalette.ColorRole.Text, QColor(COLOR_TEXT_PRIMARY))
    palette.setColor(QPalette.ColorRole.Button, QColor(COLOR_PANEL_BG))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(COLOR_TEXT_PRIMARY))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(COLOR_ACCENT))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("white"))
    app.setPalette(palette)


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    _apply_dark_palette(app)

    window = MainWindow()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()