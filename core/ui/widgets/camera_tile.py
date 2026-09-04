"""
One camera in the grid: video area plus the caption bar underneath.

The caption carries the camera name, connection status, and the motion
and detection glyphs. Frame updates are guarded by a frame counter so
an unchanged frame costs nothing -- this runs at POLL_INTERVAL_MS for
every visible tile.
"""


import cv2
from PyQt6.QtCore import QRect, Qt, pyqtSignal
from PyQt6.QtGui import QFont, QImage, QPixmap
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
)

from core.recording.recording_manager import RecordingStatus
from core.ui.constants import MOTION_ICON_SIZE
from core.ui.theme import (
    COLOR_BORDER,
    COLOR_CAPTION_BG,
    COLOR_CAPTION_BORDER,
    COLOR_DETECTION_BOX_STROKE,
    COLOR_STATUS_ERROR,
    COLOR_TEXT_PRIMARY,
    COLOR_TILE_BG,
)

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
from core.ui.widgets.icons import (
    _detection_icon_pixmap,
    _motion_icon_pixmap,
    _status_text_and_color,
)
from core.ui.widgets.video_label import VideoLabel


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
