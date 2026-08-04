"""
camera_store.py

Handles saving/loading the list of configured cameras to a local JSON file
so they persist between app launches.

Each camera is a dict:
{
    "id": "cam_1",
    "name": "Front Door",
    "url": "rtsp://192.168.1.10:554/stream1",
    "type": "rtsp",  # one of: rtsp, tcp, udp, http (informational only)
    # Phase 3 motion detection settings -- both default-filled by
    # get_camera()/list_cameras() callers reading via .get() with a
    # fallback, the same missing-key-defaults-gracefully pattern used
    # for "zones" below, so cameras created before Phase 3's tuning UI
    # existed don't break.
    "motion_enabled": True,        # master per-camera on/off switch
    "motion_sensitivity": "medium",  # "low" | "medium" | "high"
    # Phase 4 object detection settings -- same missing-key-defaults-
    # gracefully pattern as everything else in this file.
    "object_detection_enabled": False,   # master per-camera on/off switch (opt-in)
    "object_detection_mode": "on_motion",  # "on_motion" | "continuous"
    "object_detection_classes": ["person"],  # COCO class names to keep
    # Multi-instance detection QoL addition: per-class confidence
    # threshold, per camera -- e.g. this camera might want "person" at
    # 70% (busy street, lots of false positives at lower confidence)
    # but "car" at 40% (rarely wrong, want to catch more of them). A
    # class with no entry here falls back to
    # CameraStore.DEFAULT_CLASS_CONFIDENCE. Deliberately keyed by
    # class name rather than living inside object_detection_classes
    # itself (which stays a plain list) -- keeps the "which classes am
    # I even looking for" question and the "how confident do I need to
    # be about each one" question as two independent, separately-
    # editable settings, matching how the Settings UI presents them
    # (a checkbox plus an adjacent, independently-enabled spinbox).
    "object_detection_class_confidence": {
        "person": 0.7,
    },
    # Phase 5 alerting settings -- same missing-key-defaults-gracefully
    # pattern as everything else in this file. Rules are per-camera
    # (not per-zone -- see PROJECT_ROADMAP.md Phase 5 design notes).
    "alert_rules": [
        {
            "id": "rule_1",
            "name": "Night -- any motion",
            "enabled": True,
            "start": "22:00",   # HH:MM local time
            "end": "06:00",     # end < start means the window crosses midnight
            "trigger_type": "motion",       # "motion" | "object_class"
            "classes": [],                  # only meaningful for "object_class"
        },
        ...
    ],
    "zones": [
        {
            "id": "zone_1",
            "name": "Entrance",
            # Polygon points normalized to 0.0-1.0 (fraction of frame
            # width/height) so zones stay valid across resizes and
            # resolution changes. At least 3 points to be a closed shape.
            "points": [[0.1, 0.1], [0.4, 0.1], [0.4, 0.5], [0.1, 0.5]],
            # Phase 3: per-zone opt-in for zone-restricted motion
            # detection. False by default (opt-in, not opt-out) --
            # drawing a zone is about defining a region of interest for
            # many possible future uses, it shouldn't silently start
            # gating motion detection the moment it's created.
            "detection_enabled": False,
        },
        ...
    ]
}

Cameras created before zones existed simply won't have a "zones" key --
get_zones() treats a missing key the same as an empty list, so nothing
breaks for old data.
"""

import json
import os
import uuid

DEFAULT_STORE_PATH = os.path.join(os.path.expanduser("~"), ".cctv_viewer_cameras.json")


class CameraStore:
    # Matches object_detector.py's own historical default (the module
    # constant that used to be the single global confidence gate
    # before per-class thresholds existed) -- kept here too since this
    # store needs its own fallback independent of that module (no
    # import between them; object_detector.py depends on this store,
    # not the other way around).
    DEFAULT_CLASS_CONFIDENCE = 0.4

    def __init__(self, path=None):
        self.path = path or DEFAULT_STORE_PATH
        self.cameras = []
        self.load()

    def load(self):
        """Load camera list from disk. If the file doesn't exist or is
        corrupt, start with an empty list instead of crashing."""
        if not os.path.exists(self.path):
            self.cameras = []
            return

        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                self.cameras = data
            else:
                self.cameras = []
        except (json.JSONDecodeError, OSError):
            # Corrupt or unreadable file -- don't crash the app, just
            # start fresh. The bad file is left on disk in case the user
            # wants to inspect it manually.
            self.cameras = []

    def save(self):
        """Persist the current camera list to disk."""
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.cameras, f, indent=2)

    def list_cameras(self):
        """Return a shallow copy so callers can't mutate internal state
        without going through add/update/remove."""
        return list(self.cameras)

    def get_camera(self, cam_id):
        for cam in self.cameras:
            if cam["id"] == cam_id:
                return cam
        return None

    def add_camera(self, name, url, cam_type="rtsp"):
        name = (name or "").strip()
        url = (url or "").strip()
        if not name:
            raise ValueError("Camera name cannot be empty.")
        if not url:
            raise ValueError("Camera URL cannot be empty.")

        cam = {
            "id": uuid.uuid4().hex[:8],
            "name": name,
            "url": url,
            "type": cam_type,
        }
        self.cameras.append(cam)
        self.save()
        return cam

    def update_camera(self, cam_id, name=None, url=None, cam_type=None):
        cam = self.get_camera(cam_id)
        if cam is None:
            raise ValueError(f"No camera found with id {cam_id}")

        if name is not None:
            name = name.strip()
            if not name:
                raise ValueError("Camera name cannot be empty.")
            cam["name"] = name

        if url is not None:
            url = url.strip()
            if not url:
                raise ValueError("Camera URL cannot be empty.")
            cam["url"] = url

        if cam_type is not None:
            cam["type"] = cam_type

        self.save()
        return cam

    def remove_camera(self, cam_id):
        before = len(self.cameras)
        self.cameras = [c for c in self.cameras if c["id"] != cam_id]
        removed = len(self.cameras) != before
        if removed:
            self.save()
        return removed

    # ----- motion detection settings (Phase 3 tuning) -------------------
    #
    # Camera-level master switch + sensitivity. Defaults handled via
    # .get() with a fallback wherever these are read, not by migrating
    # old camera dicts on load -- same approach as "zones" already
    # uses, so a camera saved before this feature existed just works.

    VALID_SENSITIVITIES = ("low", "medium", "high")
    DEFAULT_SENSITIVITY = "medium"

    def get_motion_enabled(self, cam_id):
        cam = self.get_camera(cam_id)
        if cam is None:
            raise ValueError(f"No camera found with id {cam_id}")
        return cam.get("motion_enabled", True)

    def set_motion_enabled(self, cam_id, enabled):
        cam = self.get_camera(cam_id)
        if cam is None:
            raise ValueError(f"No camera found with id {cam_id}")
        cam["motion_enabled"] = bool(enabled)
        self.save()
        return cam

    def get_motion_sensitivity(self, cam_id):
        cam = self.get_camera(cam_id)
        if cam is None:
            raise ValueError(f"No camera found with id {cam_id}")
        return cam.get("motion_sensitivity", self.DEFAULT_SENSITIVITY)

    def set_motion_sensitivity(self, cam_id, sensitivity):
        cam = self.get_camera(cam_id)
        if cam is None:
            raise ValueError(f"No camera found with id {cam_id}")
        if sensitivity not in self.VALID_SENSITIVITIES:
            raise ValueError(
                f"Sensitivity must be one of {self.VALID_SENSITIVITIES}, got {sensitivity!r}"
            )
        cam["motion_sensitivity"] = sensitivity
        self.save()
        return cam

    # ----- object detection settings (Phase 4 tuning) --------------------
    #
    # Camera-level master switch + trigger mode + class allowlist.
    # Same missing-key-defaults-gracefully pattern as motion settings
    # above -- a camera saved before Phase 4 existed just works, opting
    # in to detection (False) with sane defaults for mode/classes.

    VALID_DETECTION_MODES = ("on_motion", "continuous")
    DEFAULT_DETECTION_MODE = "on_motion"
    DEFAULT_DETECTION_CLASSES = ["person"]

    def get_object_detection_enabled(self, cam_id):
        cam = self.get_camera(cam_id)
        if cam is None:
            raise ValueError(f"No camera found with id {cam_id}")
        return cam.get("object_detection_enabled", False)

    def set_object_detection_enabled(self, cam_id, enabled):
        cam = self.get_camera(cam_id)
        if cam is None:
            raise ValueError(f"No camera found with id {cam_id}")
        cam["object_detection_enabled"] = bool(enabled)
        self.save()
        return cam

    def get_object_detection_mode(self, cam_id):
        cam = self.get_camera(cam_id)
        if cam is None:
            raise ValueError(f"No camera found with id {cam_id}")
        return cam.get("object_detection_mode", self.DEFAULT_DETECTION_MODE)

    def set_object_detection_mode(self, cam_id, mode):
        cam = self.get_camera(cam_id)
        if cam is None:
            raise ValueError(f"No camera found with id {cam_id}")
        if mode not in self.VALID_DETECTION_MODES:
            raise ValueError(
                f"Mode must be one of {self.VALID_DETECTION_MODES}, got {mode!r}"
            )
        cam["object_detection_mode"] = mode
        self.save()
        return cam

    def get_object_detection_classes(self, cam_id):
        cam = self.get_camera(cam_id)
        if cam is None:
            raise ValueError(f"No camera found with id {cam_id}")
        return list(cam.get("object_detection_classes", self.DEFAULT_DETECTION_CLASSES))

    def set_object_detection_classes(self, cam_id, classes):
        cam = self.get_camera(cam_id)
        if cam is None:
            raise ValueError(f"No camera found with id {cam_id}")
        cleaned = [str(c).strip() for c in (classes or []) if str(c).strip()]
        cam["object_detection_classes"] = cleaned
        self.save()
        return cam

    # ----- per-class confidence thresholds (multi-instance QoL) --------
    #
    # A class's confidence gate is per-camera, not global -- the same
    # class might need a stricter threshold on one camera (busy scene,
    # more false positives) than another. Defaults-gracefully to
    # DEFAULT_CLASS_CONFIDENCE for any class without an explicit entry,
    # same pattern as every other per-camera setting in this file.

    def get_class_confidence_thresholds(self, cam_id):
        """Return a shallow copy of this camera's {class_name:
        threshold} overrides (not filled in for every candidate class
        -- only classes the user has actually adjusted away from the
        default appear here). Use get_class_confidence() for a single
        class's effective threshold including the fallback."""
        cam = self.get_camera(cam_id)
        if cam is None:
            raise ValueError(f"No camera found with id {cam_id}")
        return dict(cam.get("object_detection_class_confidence", {}))

    def get_class_confidence(self, cam_id, class_name):
        """This camera's effective confidence threshold for one class
        -- its explicit override if set, else DEFAULT_CLASS_CONFIDENCE.
        This is what object_detector.py actually calls per detected
        box, once per inference call."""
        cam = self.get_camera(cam_id)
        if cam is None:
            raise ValueError(f"No camera found with id {cam_id}")
        overrides = cam.get("object_detection_class_confidence", {})
        return float(overrides.get(class_name, self.DEFAULT_CLASS_CONFIDENCE))

    def set_class_confidence_threshold(self, cam_id, class_name, threshold):
        cam = self.get_camera(cam_id)
        if cam is None:
            raise ValueError(f"No camera found with id {cam_id}")
        threshold = float(threshold)
        if not (0.0 <= threshold <= 1.0):
            raise ValueError(f"Confidence threshold must be between 0.0 and 1.0, got {threshold!r}")
        cam.setdefault("object_detection_class_confidence", {})[class_name] = threshold
        self.save()
        return cam

    # ----- alert rules (Phase 5) ----------------------------------------
    #
    # Per-camera list of time-of-day alert rules. Deliberately NOT
    # per-zone (kept simple per the Phase 5 design discussion) -- a
    # rule matches either "any motion on this camera" (whole-frame OR
    # any zone -- see alert_matcher.py's note on why both must be
    # checked) or "this object class was seen," within a time window.
    #
    # can_use_object_class_trigger() is the same single-chokepoint
    # pattern as can_add_zone() above: a future tier system (motion
    # alerts on every tier, object-class alerts gated to a paid tier)
    # only needs to change this one method, plus whatever UI wants to
    # proactively disable that option ahead of time. Always True today
    # -- no tiers exist yet.

    VALID_TRIGGER_TYPES = ("motion", "object_class")

    def can_use_object_class_trigger(self, cam_id):
        return True

    def get_alert_rules(self, cam_id):
        """Return a shallow copy of the alert rule list for a camera
        (empty list if none yet, including cameras saved before
        alerting existed)."""
        cam = self.get_camera(cam_id)
        if cam is None:
            raise ValueError(f"No camera found with id {cam_id}")
        return list(cam.get("alert_rules", []))

    def get_alert_rule(self, cam_id, rule_id):
        for rule in self.get_alert_rules(cam_id):
            if rule["id"] == rule_id:
                return rule
        return None

    @staticmethod
    def _validate_hhmm(value, field_label):
        value = (value or "").strip()
        parts = value.split(":")
        if len(parts) != 2:
            raise ValueError(f"{field_label} must be in HH:MM format, got {value!r}")
        try:
            h, m = int(parts[0]), int(parts[1])
        except ValueError:
            raise ValueError(f"{field_label} must be in HH:MM format, got {value!r}")
        if not (0 <= h <= 23 and 0 <= m <= 59):
            raise ValueError(f"{field_label} must be a valid 24h time, got {value!r}")
        return f"{h:02d}:{m:02d}"

    def add_alert_rule(self, cam_id, name, start, end, trigger_type, classes=None, enabled=True):
        cam = self.get_camera(cam_id)
        if cam is None:
            raise ValueError(f"No camera found with id {cam_id}")

        name = (name or "").strip()
        if not name:
            raise ValueError("Rule name cannot be empty.")

        if trigger_type not in self.VALID_TRIGGER_TYPES:
            raise ValueError(
                f"trigger_type must be one of {self.VALID_TRIGGER_TYPES}, got {trigger_type!r}"
            )
        if trigger_type == "object_class" and not self.can_use_object_class_trigger(cam_id):
            raise ValueError("Object-class alert triggers aren't available for this camera.")

        start = self._validate_hhmm(start, "Start time")
        end = self._validate_hhmm(end, "End time")

        cleaned_classes = [str(c).strip() for c in (classes or []) if str(c).strip()]

        rule = {
            "id": uuid.uuid4().hex[:8],
            "name": name,
            "enabled": bool(enabled),
            "start": start,
            "end": end,
            "trigger_type": trigger_type,
            "classes": cleaned_classes,
        }
        cam.setdefault("alert_rules", []).append(rule)
        self.save()
        return rule

    def update_alert_rule(self, cam_id, rule_id, name=None, start=None, end=None,
                           trigger_type=None, classes=None, enabled=None):
        rule = self.get_alert_rule(cam_id, rule_id)
        if rule is None:
            raise ValueError(f"No alert rule found with id {rule_id}")

        if name is not None:
            name = name.strip()
            if not name:
                raise ValueError("Rule name cannot be empty.")
            rule["name"] = name

        if trigger_type is not None:
            if trigger_type not in self.VALID_TRIGGER_TYPES:
                raise ValueError(
                    f"trigger_type must be one of {self.VALID_TRIGGER_TYPES}, got {trigger_type!r}"
                )
            if trigger_type == "object_class" and not self.can_use_object_class_trigger(cam_id):
                raise ValueError("Object-class alert triggers aren't available for this camera.")
            rule["trigger_type"] = trigger_type

        if start is not None:
            rule["start"] = self._validate_hhmm(start, "Start time")
        if end is not None:
            rule["end"] = self._validate_hhmm(end, "End time")

        if classes is not None:
            rule["classes"] = [str(c).strip() for c in classes if str(c).strip()]

        if enabled is not None:
            rule["enabled"] = bool(enabled)

        self.save()
        return rule

    def remove_alert_rule(self, cam_id, rule_id):
        cam = self.get_camera(cam_id)
        if cam is None:
            raise ValueError(f"No camera found with id {cam_id}")

        rules = cam.get("alert_rules", [])
        before = len(rules)
        cam["alert_rules"] = [r for r in rules if r["id"] != rule_id]
        removed = len(cam["alert_rules"]) != before
        if removed:
            self.save()
        return removed

    # ----- zones ------------------------------------------------------
    #
    # All zone creation funnels through add_zone(), which itself funnels
    # through can_add_zone(). That's deliberate: it's the one place a
    # future tier/plan limit ("free tier = 2 zones per camera") gets
    # enforced, instead of needing to be checked in every UI path that
    # might create a zone. For now can_add_zone() always allows it.

    def get_zones(self, cam_id):
        """Return a shallow copy of the zone list for a camera (empty
        list if the camera has none yet, including cameras saved before
        zones existed)."""
        cam = self.get_camera(cam_id)
        if cam is None:
            raise ValueError(f"No camera found with id {cam_id}")
        return list(cam.get("zones", []))

    def get_zone(self, cam_id, zone_id):
        for zone in self.get_zones(cam_id):
            if zone["id"] == zone_id:
                return zone
        return None

    def can_add_zone(self, cam_id):
        """Hook for future plan/tier enforcement (e.g. 'free tier allows
        2 zones per camera'). Always True for now -- no limits exist
        yet. Centralizing the check here means a future limit only
        needs to change this one method, plus whatever UI wants to
        proactively disable the "add zone" action ahead of time."""
        return True

    def add_zone(self, cam_id, name, points):
        """Add a polygon zone to a camera.

        points: list of (x, y) pairs, normalized 0.0-1.0, in the order
        the user drew them. Needs at least 3 points to form a polygon.
        """
        cam = self.get_camera(cam_id)
        if cam is None:
            raise ValueError(f"No camera found with id {cam_id}")

        if not self.can_add_zone(cam_id):
            raise ValueError("Zone limit reached for this camera.")

        name = (name or "").strip()
        if not name:
            raise ValueError("Zone name cannot be empty.")

        points = [[float(x), float(y)] for x, y in points]
        if len(points) < 3:
            raise ValueError("A zone needs at least 3 points.")
        for x, y in points:
            if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
                raise ValueError("Zone points must be normalized between 0.0 and 1.0.")

        zone = {
            "id": uuid.uuid4().hex[:8],
            "name": name,
            "points": points,
            # Opt-in, off by default -- see module docstring.
            "detection_enabled": False,
        }
        cam.setdefault("zones", []).append(zone)
        self.save()
        return zone

    def update_zone(self, cam_id, zone_id, name=None, points=None, detection_enabled=None):
        zone = self.get_zone(cam_id, zone_id)
        if zone is None:
            raise ValueError(f"No zone found with id {zone_id}")

        if name is not None:
            name = name.strip()
            if not name:
                raise ValueError("Zone name cannot be empty.")
            zone["name"] = name

        if points is not None:
            points = [[float(x), float(y)] for x, y in points]
            if len(points) < 3:
                raise ValueError("A zone needs at least 3 points.")
            for x, y in points:
                if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
                    raise ValueError("Zone points must be normalized between 0.0 and 1.0.")
            zone["points"] = points

        if detection_enabled is not None:
            zone["detection_enabled"] = bool(detection_enabled)

        self.save()
        return zone

    def remove_zone(self, cam_id, zone_id):
        cam = self.get_camera(cam_id)
        if cam is None:
            raise ValueError(f"No camera found with id {cam_id}")

        zones = cam.get("zones", [])
        before = len(zones)
        cam["zones"] = [z for z in zones if z["id"] != zone_id]
        removed = len(cam["zones"]) != before
        if removed:
            self.save()
        return removed