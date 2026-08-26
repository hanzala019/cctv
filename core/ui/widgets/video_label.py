"""
The QLabel subclass that actually paints a video frame.

Owns frame scaling and the zone / detection-box overlays. Knows nothing
about cameras, stores or detection -- it is handed pixels and polygons
and paints them. Keep it that way; if this file ever needs to import
camera_store, the logic belongs in a view instead.
"""


from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QBrush, QPainter, QPen, QPolygonF
from PyQt6.QtWidgets import (
    QLabel,
)

from core.ui.theme import (
    COLOR_DETECTION_BOX_LABEL_BG,
    COLOR_DETECTION_BOX_LABEL_TEXT,
    COLOR_DETECTION_BOX_STROKE,
    COLOR_ZONE_MOTION_FILL,
    COLOR_ZONE_MOTION_STROKE,
    COLOR_ZONE_OVERLAY_LABEL_BG,
    COLOR_ZONE_OVERLAY_LABEL_TEXT,
    COLOR_ZONE_OVERLAY_STROKE,
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
