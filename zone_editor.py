"""
zone_editor.py

QGraphicsView-based widget for defining polygon zones on a single
camera's live feed (Phase 2).

Why QGraphicsView instead of QPainter-on-a-QLabel: zones need to be
clickable/draggable after they're drawn (to reshape them), and
QGraphicsScene gives us that almost for free via QGraphicsItem's
built-in mouse handling and z-ordering, instead of us hand-rolling hit
testing. The tradeoff is more upfront wiring, which is what this file
is.

Coordinate model
-----------------
The live frame is drawn into the scene at a fixed logical size
(FRAME_SCENE_W x FRAME_SCENE_H, defined below) regardless of the
camera's actual resolution or the widget's on-screen size --
QGraphicsView handles the on-screen scaling for us via fitInView().
Zone points are stored normalized to 0.0-1.0 (fraction of frame
width/height), so:

    scene_x = norm_x * FRAME_SCENE_W
    scene_y = norm_y * FRAME_SCENE_H

This means zones survive widget resizes, window resizes, and even a
camera switching to a different resolution, since 0.0-1.0 is
resolution-independent. We only convert to/from normalized space at
the storage boundary (when loading from CameraStore / saving back).

Interaction model
------------------
Two modes, toggled by the parent view:

- VIEW mode: just displays the live feed + any saved zones as static
  overlays. No mouse interaction with zones.
- EDIT mode:
    - Click on empty space starts/continues drawing a new polygon
      (each click drops a point).
    - Click near the first point (or press Enter/double-click) closes
      the polygon, finalizing it as a new zone.
    - Press Escape to cancel an in-progress polygon.
    - Existing zones show draggable handles at each vertex; dragging a
      handle reshapes that zone live.
    - Ctrl+click on a zone's edge inserts a new point there.
    - Right-click a vertex handle shows a small "Delete point" option
      (refused if the zone only has 3 points left -- a polygon needs
      at least 3 to stay a polygon).
    - Click anywhere on a zone's polygon (not just its handles)
      selects/highlights it -- see set_highlighted_zone(). This works
      in both modes, since the Zones settings section uses it in
      view-only mode (no drawing, just select-to-highlight, but with
      handles also visible for point editing).
    - Each existing zone has a small delete glyph near its first point.
"""

import math

from PyQt6.QtCore import Qt, QPointF, QRectF, pyqtSignal
from PyQt6.QtGui import (
    QImage,
    QPixmap,
    QPolygonF,
    QPen,
    QBrush,
    QColor,
    QFont,
    QPainter,
)
from PyQt6.QtWidgets import (
    QGraphicsView,
    QGraphicsScene,
    QGraphicsPixmapItem,
    QGraphicsPolygonItem,
    QGraphicsEllipseItem,
    QGraphicsSimpleTextItem,
    QGraphicsItem,
    QMenu,
)

import cv2

# Fixed logical scene size that the frame is drawn into. Arbitrary but
# generous resolution -- big enough that vertex handles don't feel
# chunky relative to the frame, small enough to stay cheap to render.
FRAME_SCENE_W = 1280
FRAME_SCENE_H = 720

HANDLE_RADIUS = 6
CLOSE_POINT_TOLERANCE = 12  # scene units; how close a click must be to
                            # the first point to count as "closing"

COLOR_ZONE_STROKE = QColor("#4a9eff")
COLOR_ZONE_FILL = QColor(74, 158, 255, 50)       # translucent accent
COLOR_DRAFT_STROKE = QColor("#e8b339")           # amber while drawing
COLOR_DRAFT_FILL = QColor(232, 179, 57, 40)
COLOR_HANDLE = QColor("#e8e9eb")
COLOR_HANDLE_BORDER = QColor("#15171a")
COLOR_DELETE_BG = QColor("#e0524a")
COLOR_DELETE_TEXT = QColor("white")
COLOR_LABEL_BG = QColor(0, 0, 0, 160)
COLOR_LABEL_TEXT = QColor("#e8e9eb")

# Highlight state -- used when a zone is selected (from the zone list
# in the Zones settings section, or by clicking the zone on canvas).
# Brighter stroke + stronger fill than the normal resting state, and
# unselected zones dim slightly so the selected one reads clearly.
COLOR_ZONE_HIGHLIGHT_STROKE = QColor("#ffd24a")
COLOR_ZONE_HIGHLIGHT_FILL = QColor(255, 210, 74, 70)
COLOR_ZONE_DIMMED_STROKE = QColor(74, 158, 255, 120)
COLOR_ZONE_DIMMED_FILL = QColor(74, 158, 255, 20)

# Phase 3.5: a zone with detection_enabled=True gets its own resting-
# state color (instead of the default blue) so it's recognizable at a
# glance which zones actually feed motion detection vs. which are just
# regions of interest with detection still off. Deliberately a
# different hue from main.py's COLOR_ZONE_MOTION_STROKE (the "this zone
# has motion firing RIGHT NOW" color used in the live single-view
# overlay) -- "configured for detection" and "actively triggering" are
# different signals and shouldn't share a color. Selection/dimming
# still take priority when active (see ZoneItem._apply_pen_and_brush)
# since those reflect what you're actively doing with the zone right
# now, which matters more in the moment than its detection config.
COLOR_ZONE_DETECTION_STROKE = QColor("#2fc9c9")
COLOR_ZONE_DETECTION_FILL = QColor(47, 201, 201, 45)

MIN_ZONE_POINTS = 3  # a polygon needs at least 3 points to stay a polygon
EDGE_INSERT_TOLERANCE = 10  # scene units; how close a Ctrl+click must be to an edge


def _to_scene_point(norm_x, norm_y):
    return QPointF(norm_x * FRAME_SCENE_W, norm_y * FRAME_SCENE_H)


def _to_norm_point(scene_point):
    return (scene_point.x() / FRAME_SCENE_W, scene_point.y() / FRAME_SCENE_H)


class _ClickablePolygon(QGraphicsPolygonItem):
    """QGraphicsPolygonItem subclass that reports clicks back to its
    owning ZoneItem, so clicking anywhere on a zone's filled body (not
    just a vertex handle) selects/highlights it."""

    def __init__(self, zone_item, parent_view, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.zone_item = zone_item
        self.parent_view = parent_view
        self.setAcceptedMouseButtons(Qt.MouseButton.LeftButton)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            # Ctrl+Click edge insertion is now handled globally by the View to 
            # allow clicking slightly outside the strict polygon boundary.
            # So if we reach here, it's just a normal click to select the polygon.
            self.parent_view.select_zone(self.zone_item.zone_id)
            event.accept()
            return
        super().mousePressEvent(event)


class _VertexHandle(QGraphicsEllipseItem):
    """A draggable circle at one vertex of an existing zone polygon.
    Dragging it calls back to the owning ZoneItem so the polygon shape
    updates live."""

    def __init__(self, zone_item, point_index, parent_view):
        r = HANDLE_RADIUS
        super().__init__(-r, -r, 2 * r, 2 * r)
        self.zone_item = zone_item
        self.point_index = point_index
        self.parent_view = parent_view

        self.setBrush(QBrush(COLOR_HANDLE))
        self.setPen(QPen(COLOR_HANDLE_BORDER, 1.5))
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges, True)
        self.setZValue(10)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAcceptHoverEvents(True)

    def itemChange(self, change, value):
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionChange and self.scene() is not None:
            # Clamp the handle (and therefore the vertex) to the frame
            # bounds so you can't drag a zone point off-frame.
            new_pos = value
            x = min(max(new_pos.x(), 0), FRAME_SCENE_W)
            y = min(max(new_pos.y(), 0), FRAME_SCENE_H)
            clamped = QPointF(x, y)
            self.zone_item.update_point(self.point_index, clamped)
            return clamped
        return super().itemChange(change, value)

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        # Drag is finished -- tell the view so it can persist the new
        # shape. Intermediate positions during the drag only update the
        # on-screen polygon (via itemChange above); we don't want to
        # hit the store on every pixel of movement.
        self.parent_view.notify_zone_modified(self.zone_item)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.RightButton:
            # Right-click selects this point's zone too, so the
            # highlight follows wherever you're working.
            self.parent_view.select_zone(self.zone_item.zone_id)
            self._show_delete_menu(event)
            event.accept()
            return
        super().mousePressEvent(event)

    def _show_delete_menu(self, event):
        menu = QMenu()

        # PyQt6 versions differ: some return QPoint, some QPointF.
        pos = event.screenPos()
        if hasattr(pos, "toPoint"):
            pos = pos.toPoint()

        if len(self.zone_item.points) <= MIN_ZONE_POINTS:
            action = menu.addAction(
                f"Delete point (min {MIN_ZONE_POINTS} required)"
            )
            action.setEnabled(False)
            menu.exec(pos)
            return

        delete_action = menu.addAction("Delete point")
        chosen = menu.exec(pos)

        if chosen is delete_action:
            self.parent_view.delete_zone_point(
                self.zone_item,
                self.point_index,
            )


class _DeleteGlyph(QGraphicsSimpleTextItem):
    """Small clickable '✕' near a zone's first vertex to delete that
    zone entirely."""

    def __init__(self, zone_item, parent_view):
        super().__init__("\u2715")
        self.zone_item = zone_item
        self.parent_view = parent_view
        font = QFont()
        font.setBold(True)
        font.setPointSize(10)
        self.setFont(font)
        self.setBrush(QBrush(COLOR_DELETE_TEXT))
        self.setZValue(11)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAcceptHoverEvents(True)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.parent_view._request_delete_zone(self.zone_item)
            event.accept()
            return
        super().mousePressEvent(event)

    def paint(self, painter, option, widget=None):
        # Background chip so the glyph is legible over busy video.
        rect = self.boundingRect().adjusted(-4, -2, 4, 2)
        painter.setBrush(QBrush(COLOR_DELETE_BG))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(rect)
        super().paint(painter, option, widget)


class ZoneItem:
    """Groups together the polygon graphic, vertex handles, label, and
    delete glyph for one saved zone. Not a QGraphicsItem itself --
    just a convenience wrapper holding references to the actual scene
    items so we can update/remove them together.

    zone_id is None for an in-progress (not yet saved) draft polygon.
    """

    def __init__(self, scene, parent_view, zone_id, name, scene_points, draft=False, detection_enabled=False):
        self.scene = scene
        self.parent_view = parent_view
        self.zone_id = zone_id
        self.name = name
        self.points = list(scene_points)  # list[QPointF], in scene space
        self.draft = draft
        self.detection_enabled = detection_enabled
        self.highlight_state = "normal"  # "normal" | "highlighted" | "dimmed"

        self.polygon_item = _ClickablePolygon(self, parent_view, QPolygonF(self.points))
        self.polygon_item.setZValue(5)
        scene.addItem(self.polygon_item)

        self.handles = []
        self.label_item = None
        self.delete_glyph = None

        if not draft:
            self._rebuild_handles()

            self.label_item = QGraphicsSimpleTextItem(name)
            label_font = QFont()
            label_font.setBold(True)
            label_font.setPointSize(10)
            self.label_item.setFont(label_font)
            self.label_item.setBrush(QBrush(COLOR_LABEL_TEXT))
            self.label_item.setZValue(9)
            scene.addItem(self.label_item)

            self.delete_glyph = _DeleteGlyph(self, parent_view)
            scene.addItem(self.delete_glyph)

            self._reposition_label_and_glyph()

        self._apply_pen_and_brush()

    def _rebuild_handles(self):
        """Recreate vertex handles from scratch. Needed after insert/
        remove since handle.point_index values shift -- simpler and
        less error-prone than trying to patch indices in place."""
        for h in self.handles:
            self.scene.removeItem(h)
        self.handles = []
        edit_mode = getattr(self.parent_view, "edit_mode", True)
        for i, pt in enumerate(self.points):
            handle = _VertexHandle(self, i, self.parent_view)
            handle.setPos(pt)
            handle.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, edit_mode)
            handle.setVisible(edit_mode)
            self.scene.addItem(handle)
            self.handles.append(handle)

    def _apply_pen_and_brush(self):
        if self.draft:
            stroke, fill = COLOR_DRAFT_STROKE, COLOR_DRAFT_FILL
        elif self.highlight_state == "highlighted":
            stroke, fill = COLOR_ZONE_HIGHLIGHT_STROKE, COLOR_ZONE_HIGHLIGHT_FILL
        elif self.highlight_state == "dimmed":
            stroke, fill = COLOR_ZONE_DIMMED_STROKE, COLOR_ZONE_DIMMED_FILL
        elif self.detection_enabled:
            # Resting state, but this zone actually feeds motion
            # detection -- distinct teal instead of the default blue,
            # so it's recognizable at a glance in the Zones settings
            # canvas (and in SingleView's "Define Zones" canvas, which
            # shares this same code path).
            stroke, fill = COLOR_ZONE_DETECTION_STROKE, COLOR_ZONE_DETECTION_FILL
        else:
            stroke, fill = COLOR_ZONE_STROKE, COLOR_ZONE_FILL

        width = 3 if self.highlight_state == "highlighted" else 2
        self.polygon_item.setPen(QPen(stroke, width))
        self.polygon_item.setBrush(QBrush(fill))

    def set_highlight_state(self, state):
        """state: 'normal' | 'highlighted' | 'dimmed'."""
        self.highlight_state = state
        self._apply_pen_and_brush()

    def set_detection_enabled(self, enabled):
        """Update this zone's detection-enabled visual state without a
        full scene rebuild -- mirrors how set_highlight_state() updates
        color in place. Called when the Zones settings panel's
        checkbox is toggled."""
        self.detection_enabled = enabled
        self._apply_pen_and_brush()

    def _reposition_label_and_glyph(self):
        if not self.points:
            return
        anchor = self.points[0]
        if self.label_item is not None:
            self.label_item.setPos(anchor.x() + 10, anchor.y() - 22)
        if self.delete_glyph is not None:
            self.delete_glyph.setPos(anchor.x() - 18, anchor.y() - 22)

    def update_point(self, index, new_scene_pos):
        self.points[index] = new_scene_pos
        self.polygon_item.setPolygon(QPolygonF(self.points))
        if index == 0:
            self._reposition_label_and_glyph()

    def insert_point(self, index, scene_pos):
        """Insert a new point at `index` (so it becomes the point
        between the two vertices that previously shared that edge)."""
        self.points.insert(index, scene_pos)
        self.polygon_item.setPolygon(QPolygonF(self.points))
        self._rebuild_handles()
        if index == 0:
            self._reposition_label_and_glyph()

    def remove_point(self, index):
        """Remove the point at `index`. Caller is responsible for
        enforcing the minimum-points guard before calling this."""
        if len(self.points) <= MIN_ZONE_POINTS:
            return False
        del self.points[index]
        self.polygon_item.setPolygon(QPolygonF(self.points))
        self._rebuild_handles()
        self._reposition_label_and_glyph()
        return True

    def append_point(self, scene_pos):
        self.points.append(scene_pos)
        self.polygon_item.setPolygon(QPolygonF(self.points))

    def set_points_preview(self, points_including_cursor):
        """Used while drawing: show the polygon as it would look if the
        user clicked right now, including a live segment to the cursor."""
        self.polygon_item.setPolygon(QPolygonF(points_including_cursor))

    def normalized_points(self):
        return [_to_norm_point(p) for p in self.points]

    def remove_from_scene(self):
        self.scene.removeItem(self.polygon_item)
        for h in self.handles:
            self.scene.removeItem(h)
        if self.label_item is not None:
            self.scene.removeItem(self.label_item)
        if self.delete_glyph is not None:
            self.scene.removeItem(self.delete_glyph)


def _point_to_segment_distance(p, a, b):
    """Shortest distance from point p to the line segment a-b (all
    QPointF, scene coordinates)."""
    ax, ay = a.x(), a.y()
    bx, by = b.x(), b.y()
    px, py = p.x(), p.y()

    dx, dy = bx - ax, by - ay
    seg_len_sq = dx * dx + dy * dy
    if seg_len_sq == 0:
        return math.hypot(px - ax, py - ay)

    t = ((px - ax) * dx + (py - ay) * dy) / seg_len_sq
    t = max(0.0, min(1.0, t))
    closest_x = ax + t * dx
    closest_y = ay + t * dy
    return math.hypot(px - closest_x, py - closest_y)


class ZoneEditorView(QGraphicsView):
    """The widget SingleView swaps in when "Define Zones" is toggled on.

    Public API:
        set_camera(camera)        -- load this camera's saved zones
        update_frame(frame_bgr)   -- push the latest decoded frame in
        set_edit_mode(enabled, allow_new_zones=True) -- toggle drawing/
                                      editing vs. view-only. allow_new_zones
                                      controls whether clicking empty
                                      canvas starts a new polygon (used
                                      by SingleView's "Define Zones");
                                      pass False when you want existing-
                                      zone editing (drag/insert/delete
                                      points) without accidental new-zone
                                      creation (used by the Zones
                                      settings section).
        select_zone(zone_id)      -- highlight one zone, dim the rest;
                                      None clears the highlight
        zoneFinalized (signal)    -- emitted with (name_placeholder=None,
                                      normalized_points) when a new
                                      polygon is closed; caller decides
                                      naming + persistence
        zoneDeleteRequested (signal) -- emitted with zone_id when the
                                      user clicks a zone's delete glyph
        zoneModified (signal)     -- emitted with (zone_id, normalized_points)
                                      after a drag, point insert, or point
                                      delete finishes changing a zone's shape
        zoneSelected (signal)     -- emitted with zone_id (or None) when
                                      the user clicks a zone (or empty
                                      space) to select/deselect it
    """

    zoneFinalized = pyqtSignal(list)          # list of (x, y) normalized points
    zoneDeleteRequested = pyqtSignal(str)     # zone_id
    zoneModified = pyqtSignal(str, list)      # zone_id, normalized points
    zoneSelected = pyqtSignal(object)         # zone_id (str) or None

    def __init__(self, parent=None):
        super().__init__(parent)
        self.scene_ = QGraphicsScene(0, 0, FRAME_SCENE_W, FRAME_SCENE_H)
        self.setScene(self.scene_)
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setBackgroundBrush(QBrush(QColor("#000000")))

        self.pixmap_item = QGraphicsPixmapItem()
        self.pixmap_item.setZValue(0)
        self.scene_.addItem(self.pixmap_item)

        self.camera = None
        self.edit_mode = False
        self.allow_new_zones = True
        self.selected_zone_id = None
        self._zone_items = {}      # zone_id -> ZoneItem
        self._draft_points = []    # list[QPointF] while drawing
        self._draft_item = None    # ZoneItem(draft=True) while drawing
        self._dragging_handle = False

        self.setMouseTracking(True)

    # ----- public API --------------------------------------------------

    def set_camera(self, camera):
        """Load (or reload) this camera's saved zones from its dict.
        Cancels any in-progress draft polygon."""
        self._cancel_draft()
        self.camera = camera
        self.selected_zone_id = None
        self._rebuild_zone_items()

    def update_frame(self, frame_bgr):
        if frame_bgr is None:
            return
        frame_rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        h, w, ch = frame_rgb.shape
        bytes_per_line = ch * w
        qimg = QImage(frame_rgb.data, w, h, bytes_per_line, QImage.Format.Format_RGB888)
        # Keep the source buffer alive long enough for Qt to consume it.
        self._current_qimage = qimg
        pixmap = QPixmap.fromImage(qimg).scaled(
            FRAME_SCENE_W,
            FRAME_SCENE_H,
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.pixmap_item.setPixmap(pixmap)

    def set_edit_mode(self, enabled, allow_new_zones=True):
        self.edit_mode = enabled
        self.allow_new_zones = allow_new_zones
        self._cancel_draft()
        if enabled:
            self.setCursor(Qt.CursorShape.CrossCursor if allow_new_zones else Qt.CursorShape.ArrowCursor)
        else:
            self.setCursor(Qt.CursorShape.ArrowCursor)
        for zone_item in self._zone_items.values():
            for handle in zone_item.handles:
                handle.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, enabled)
                handle.setVisible(enabled)
            if zone_item.delete_glyph is not None:
                zone_item.delete_glyph.setVisible(enabled and allow_new_zones)

    def select_zone(self, zone_id):
        """Highlight one zone and dim the others. zone_id=None clears
        the highlight (all zones back to normal). Emits zoneSelected so
        external UI (e.g. the Zones settings list) can stay in sync."""
        self.selected_zone_id = zone_id
        for zid, item in self._zone_items.items():
            if zone_id is None:
                item.set_highlight_state("normal")
            elif zid == zone_id:
                item.set_highlight_state("highlighted")
            else:
                item.set_highlight_state("dimmed")
        self.zoneSelected.emit(zone_id)

    def get_zone_snapshot(self):
        """Read-only snapshot of what's currently on canvas, as
        {zone_id: (name, [(norm_x, norm_y), ...], detection_enabled)}.
        Public accessor so callers (e.g. the Zones settings section)
        can compare canvas state against stored state without reaching
        into the private _zone_items dict directly."""
        return {
            zid: (item.name, item.normalized_points(), item.detection_enabled)
            for zid, item in self._zone_items.items()
        }

    def delete_zone_point(self, zone_item, point_index):
        removed = zone_item.remove_point(point_index)
        if removed:
            self.notify_zone_modified(zone_item)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.fitInView(QRectF(0, 0, FRAME_SCENE_W, FRAME_SCENE_H), Qt.AspectRatioMode.KeepAspectRatio)

    def showEvent(self, event):
        super().showEvent(event)
        self.fitInView(QRectF(0, 0, FRAME_SCENE_W, FRAME_SCENE_H), Qt.AspectRatioMode.KeepAspectRatio)

    # ----- internals: building zone items from camera data ------------

    def _rebuild_zone_items(self):
        for zone_item in self._zone_items.values():
            zone_item.remove_from_scene()
        self._zone_items = {}

        if self.camera is None:
            return

        for zone in self.camera.get("zones", []):
            scene_points = [_to_scene_point(x, y) for x, y in zone["points"]]
            item = ZoneItem(
                self.scene_,
                self,
                zone_id=zone["id"],
                name=zone.get("name", ""),
                scene_points=scene_points,
                draft=False,
                detection_enabled=zone.get("detection_enabled", False),
            )
            self._zone_items[zone["id"]] = item
        # Respect current mode for newly built handles/glyphs.
        self.set_edit_mode(self.edit_mode, self.allow_new_zones)
        # Re-apply any active selection highlight after the rebuild.
        if self.selected_zone_id is not None:
            self.select_zone(self.selected_zone_id)

    def refresh_zone(self, zone_id, name=None):
        """Call after persisting a rename so the on-canvas label updates
        without a full rebuild (avoids losing in-progress drag state)."""
        item = self._zone_items.get(zone_id)
        if item is not None and name is not None:
            item.name = name
            if item.label_item is not None:
                item.label_item.setText(name)

    def set_zone_detection_enabled(self, zone_id, enabled):
        """Call after persisting a detection-enabled checkbox toggle so
        the on-canvas color updates immediately without a full rebuild
        (same in-place-update pattern as refresh_zone above)."""
        item = self._zone_items.get(zone_id)
        if item is not None:
            item.set_detection_enabled(enabled)

    # ----- drawing new zones & point insertion --------------------------

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return

        clicked_item = self.itemAt(event.pos())
        
        # 1. Prioritize explicit interactions with handles/glyphs
        #    We don't want a Ctrl+Click on a handle to insert a new point directly
        #    on top of the existing handle; it should just interact with the handle.
        if isinstance(clicked_item, (_VertexHandle, _DeleteGlyph)):
            super().mousePressEvent(event)
            return

        scene_pos = self.mapToScene(event.pos())
        scene_pos.setX(min(max(scene_pos.x(), 0), FRAME_SCENE_W))
        scene_pos.setY(min(max(scene_pos.y(), 0), FRAME_SCENE_H))

        # 2. Handle Ctrl+Click edge insertion globally at the View level
        #    This fixes the issue where clicking just 1 pixel outside the strict
        #    polygon shape would miss the QGraphicsPolygonItem entirely, bypassing
        #    the insertion logic and accidentally starting a new draft.
        if self.edit_mode and (event.modifiers() & Qt.KeyboardModifier.ControlModifier):
            best_zone = None
            best_index = None
            best_dist = EDGE_INSERT_TOLERANCE

            for zone_item in self._zone_items.values():
                n = len(zone_item.points)
                for i in range(n):
                    a = zone_item.points[i]
                    b = zone_item.points[(i + 1) % n]
                    dist = _point_to_segment_distance(scene_pos, a, b)
                    if dist <= best_dist:
                        best_dist = dist
                        best_index = (i + 1) % n
                        if best_index == 0:
                            best_index = n
                        best_zone = zone_item

            if best_zone is not None:
                best_zone.insert_point(best_index, scene_pos)
                self.notify_zone_modified(best_zone)
                self.select_zone(best_zone.zone_id)
            # Accept event so we don't accidentally fall through and start a draft
            event.accept()
            return

        # 3. If they clicked the polygon body (without Ctrl), handle normal selection
        if isinstance(clicked_item, _ClickablePolygon):
            if not clicked_item.zone_item.draft and not self._draft_points:
                super().mousePressEvent(event)
                return

        # 4. Empty-space click: clear selection, start/continue drawing
        if self.selected_zone_id is not None:
            self.select_zone(None)

        if not self.edit_mode or not self.allow_new_zones:
            super().mousePressEvent(event)
            return

        if self._draft_points and self._is_near_first_point(scene_pos):
            self._finalize_draft()
            return

        self._draft_points.append(scene_pos)

        if self._draft_item is None:
            self._draft_item = ZoneItem(
                self.scene_,
                self,
                zone_id=None,
                name="",
                scene_points=self._draft_points,
                draft=True,
            )
        else:
            self._draft_item.set_points_preview(self._draft_points)

        event.accept()

    def mouseMoveEvent(self, event):
        if self.edit_mode and self._draft_points and self._draft_item is not None:
            cursor_scene = self.mapToScene(event.pos())
            preview = self._draft_points + [cursor_scene]
            self._draft_item.set_points_preview(preview)
        super().mouseMoveEvent(event)

    def mouseDoubleClickEvent(self, event):
        if self.edit_mode and len(self._draft_points) >= 3:
            self._finalize_draft()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def keyPressEvent(self, event):
        if self.edit_mode and event.key() == Qt.Key.Key_Escape:
            self._cancel_draft()
            event.accept()
            return
        if self.edit_mode and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if len(self._draft_points) >= 3:
                self._finalize_draft()
                event.accept()
                return
        super().keyPressEvent(event)

    def _is_near_first_point(self, scene_pos):
        first = self._draft_points[0]
        dx = scene_pos.x() - first.x()
        dy = scene_pos.y() - first.y()
        return math.hypot(dx, dy) <= CLOSE_POINT_TOLERANCE

    def _finalize_draft(self):
        if len(self._draft_points) < 3:
            self._cancel_draft()
            return
        normalized = [_to_norm_point(p) for p in self._draft_points]
        self._cancel_draft()
        self.zoneFinalized.emit(normalized)

    def _cancel_draft(self):
        if self._draft_item is not None:
            self._draft_item.remove_from_scene()
            self._draft_item = None
        self._draft_points = []

    # ----- deletion / modification callbacks used by child items -------

    def _request_delete_zone(self, zone_item):
        if zone_item.zone_id is not None:
            self.zoneDeleteRequested.emit(zone_item.zone_id)

    def notify_zone_modified(self, zone_item):
        if zone_item.zone_id is not None:
            self.zoneModified.emit(zone_item.zone_id, zone_item.normalized_points())

    # ----- called externally after persistence to sync state -----------

    def add_zone_to_scene(self, zone_id, name, normalized_points):
        scene_points = [_to_scene_point(x, y) for x, y in normalized_points]
        item = ZoneItem(self.scene_, self, zone_id, name, scene_points, draft=False)
        self._zone_items[zone_id] = item
        self.set_edit_mode(self.edit_mode, self.allow_new_zones)

    def remove_zone_from_scene(self, zone_id):
        item = self._zone_items.pop(zone_id, None)
        if item is not None:
            item.remove_from_scene()