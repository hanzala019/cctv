"""
MainWindow -- the wiring hub.

Owns every manager (streams, motion, detection, alerts, recording,
event logging), the view stack, and the ~30 FPS poll loop that pulls
frames and pushes them into the visible tiles.

This is the highest-conflict file in the repository: nearly every
feature wants to add a line to __init__. Keep additions to one or two
lines and push the real work into the subsystem packages.

Two things here are order-sensitive and must not be casually
rearranged:

  * _poll runs POLL_INTERVAL_MS apart for every visible tile. No
    SQLite, no disk I/O, nothing per-frame that could be per-change.
    See GUIDELINE.md section 9.
  * closeEvent stops event logging and alerts BEFORE recording,
    because both tag their closing row with
    recording.get_current_segment_id(). Reversing that loses the tag.
"""

from datetime import datetime

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from core.alerts.alert_manager import AlertManager
from core.capture.video_stream import StreamManager
from core.detection.motion_detector import MotionManager
from core.detection.object_detector import ObjectDetectionManager
from core.diagnostics import ResourceMonitor
from core.recording.event_logger import EventLoggerManager
from core.recording.recording_manager import RecordingManager
from core.storage.camera_store import CameraStore
from core.storage.event_store import EventStore, save_event_thumbnail
from core.ui.constants import POLL_INTERVAL_MS
from core.ui.settings import SettingsPanel
from core.ui.theme import (
    COLOR_BG,
    COLOR_BORDER,
    COLOR_PANEL_BG,
    COLOR_TEXT_PRIMARY,
)
from core.ui.theme import button_style as _button_style
from core.ui.views.detection_panel import DetectionSidePanel
from core.ui.views.grid_view import GridView
from core.ui.views.single_view import SingleView

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


    # ----- resource diagnostics -------------------------------------------

        # Deliberately its own timer, NOT the 30 Hz poll loop: sampling costs
        # a handful of OS reads, which is nothing every 5s and real work at
        # 30 fps per tile. See GUIDELINE.md section 9.
        self._monitor = ResourceMonitor()
        self._monitor.sample_cpu()  # prime: CPU times are cumulative, so the
                                    # first reading has no delta to report

        self._diag_timer = QTimer(self)
        self._diag_timer.timeout.connect(self._log_diagnostics)
        self._diag_timer.start(5000)  # every 5s

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
            tile.camera = cam
            tile.set_zones(cam.get("zones", []))

        if self.single_view.camera and self.single_view.camera["id"] == cam_id:
            # CameraStore returns a NEW dict on every call -- it does not
            # mutate in place, as it did when the store was JSON-backed.
            # Without this reassignment, _rebuild_body() builds the next
            # CameraTile from the snapshot taken when the view was opened,
            # so every zone edit stays invisible until the view is rebuilt
            # (i.e. until you go back to the grid).
            self.single_view.camera = cam

            if not self.single_view.zone_editing and self.single_view.tile is not None:
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

    def _log_diagnostics(self):
        """Per-camera CPU and buffer memory. Wrapped because diagnostics
        must never be able to take the app down."""
        try:
            print(self._monitor.report(managers={
                "stream": self.streams,
                "motion": self.motion,
                "detect": self.detection,
                "record": self.recording,
            }))
        except Exception as exc:
            print(f"[diagnostics] {exc}")

    def closeEvent(self, event):
        self._diag_timer.stop()
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
