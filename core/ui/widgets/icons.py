"""
Small cached pixmaps drawn in camera captions, plus the stream-status
to colour mapping.

The pixmaps are built lazily on first use and shared by every tile: a
QPixmap cannot be constructed before QApplication exists, so these
cannot be module-level constants, and the glyphs never change -- only
their visibility does.
"""

####### Whovever is working on this, you can use svgs, images or icons directly by importing them. Drawing them here is just an extra hassle
####### In case you use external assets then create a folder called assets in the ui directory, not widgets.
####### It should look like this: ui -> settings, views, widgets, assets, __init__.py, app.py, constants.py, theme.py

from PyQt6.QtCore import QPointF, Qt
from PyQt6.QtGui import QBrush, QPainter, QPen, QPixmap

from core.capture.video_stream import StreamStatus
from core.ui.constants import MOTION_ICON_SIZE
from core.ui.theme import (
    COLOR_DETECTION_BOX_STROKE,
    COLOR_MOTION_ACTIVE,
    COLOR_STATUS_CONNECTING,
    COLOR_STATUS_ERROR,
    COLOR_TEXT_MUTED,
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
