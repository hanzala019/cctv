"""
Tests for core.storage.camera_store.

CameraStore is the most load-bearing component in the project: the dict
returned by get_camera() is consumed by detection, alerts, recording and
six UI modules. A silent change to its shape breaks four people at once,
so the shape itself is asserted here, not just the behaviour.

Every test gets its own temporary database via the `store` fixture, so
tests are independent and never touch a developer's real cameras.db.
"""

import pytest

from core.storage.camera_store import CameraStore


@pytest.fixture
def store(tmp_path):
    """A CameraStore backed by a throwaway database."""
    return CameraStore(path=str(tmp_path / "cameras.db"))


@pytest.fixture
def cam(store):
    """A camera that already exists, for tests that need one."""
    return store.add_camera("Front Door", "rtsp://192.168.1.50/stream")


# ===================================================================
# The camera dict contract
# ===================================================================

EXPECTED_KEYS = {
    "id",
    "name",
    "url",
    "type",
    "motion_enabled",
    "motion_sensitivity",
    "object_detection_enabled",
    "object_detection_mode",
    "object_detection_classes",
    "object_detection_class_confidence",
    "alert_rules",
    "zones",
}


def test_camera_dict_has_exactly_the_documented_keys(cam):
    """Guards the contract in GUIDELINE.md section 4.

    If this fails, either the docs or four subsystems need updating --
    decide which deliberately, don't just update the test.
    """
    assert set(cam) == EXPECTED_KEYS, (
        f"missing: {EXPECTED_KEYS - set(cam)}, unexpected: {set(cam) - EXPECTED_KEYS}"
    )


def test_camera_dict_value_types(cam):
    assert isinstance(cam["id"], str)
    assert isinstance(cam["motion_enabled"], bool)
    assert isinstance(cam["object_detection_classes"], list)
    assert isinstance(cam["object_detection_class_confidence"], dict)
    assert isinstance(cam["alert_rules"], list)
    assert isinstance(cam["zones"], list)


def test_get_camera_matches_add_camera(store, cam):
    assert store.get_camera(cam["id"]) == cam


# ===================================================================
# CRUD
# ===================================================================

def test_new_store_is_empty(store):
    assert store.list_cameras() == []


def test_add_then_list(store):
    store.add_camera("A", "rtsp://a")
    store.add_camera("B", "rtsp://b")
    assert {c["name"] for c in store.list_cameras()} == {"A", "B"}


def test_camera_ids_are_unique(store):
    a = store.add_camera("Same Name", "rtsp://a")
    b = store.add_camera("Same Name", "rtsp://b")
    assert a["id"] != b["id"]


def test_update_camera_changes_only_what_is_passed(store, cam):
    updated = store.update_camera(cam["id"], name="Back Door")
    assert updated["name"] == "Back Door"
    assert updated["url"] == cam["url"]


def test_remove_camera(store, cam):
    store.remove_camera(cam["id"])
    assert store.list_cameras() == []
    assert store.get_camera(cam["id"]) is None


def test_get_missing_camera_returns_none(store):
    assert store.get_camera("does-not-exist") is None


def test_persistence_across_instances(tmp_path):
    """The whole point of SQLite over in-memory state."""
    path = str(tmp_path / "cameras.db")
    first = CameraStore(path=path)
    cam_id = first.add_camera("Persisted", "rtsp://x")["id"]

    second = CameraStore(path=path)
    assert second.get_camera(cam_id)["name"] == "Persisted" # type: ignore


# ===================================================================
# Validation
# ===================================================================

@pytest.mark.parametrize("bad", ["", "  ", None])
def test_add_camera_rejects_empty_name(store, bad):
    with pytest.raises(ValueError):
        store.add_camera(bad, "rtsp://x")


@pytest.mark.parametrize("bad", ["", "  ", None])
def test_add_camera_rejects_empty_url(store, bad):
    with pytest.raises(ValueError):
        store.add_camera("Name", bad)


def test_set_motion_sensitivity_rejects_unknown_value(store, cam):
    with pytest.raises(ValueError):
        store.set_motion_sensitivity(cam["id"], "extremely-high")


@pytest.mark.parametrize("sensitivity", ["low", "medium", "high"])
def test_set_motion_sensitivity_accepts_valid_values(store, cam, sensitivity):
    result = store.set_motion_sensitivity(cam["id"], sensitivity)
    assert result["motion_sensitivity"] == sensitivity


@pytest.mark.parametrize("bad", [-0.1, 1.1, 2.0])
def test_class_confidence_rejects_out_of_range(store, cam, bad):
    with pytest.raises(ValueError):
        store.set_class_confidence_threshold(cam["id"], "person", bad)


@pytest.mark.parametrize("good", [0.0, 0.5, 1.0])
def test_class_confidence_accepts_boundaries(store, cam, good):
    store.set_class_confidence_threshold(cam["id"], "person", good)
    assert store.get_class_confidence(cam["id"], "person") == good


def test_operations_on_missing_camera_raise(store):
    with pytest.raises(Exception):
        store.set_motion_enabled("nope", True)


# ===================================================================
# Detection classes
# ===================================================================

def test_set_detection_classes_round_trips(store, cam):
    store.set_object_detection_classes(cam["id"], ["person", "car"])
    assert set(store.get_object_detection_classes(cam["id"])) == {"person", "car"}


def test_set_detection_classes_replaces_rather_than_appends(store, cam):
    store.set_object_detection_classes(cam["id"], ["person", "car"])
    store.set_object_detection_classes(cam["id"], ["dog"])
    assert store.get_object_detection_classes(cam["id"]) == ["dog"]


def test_set_detection_classes_strips_and_drops_blanks(store, cam):
    store.set_object_detection_classes(cam["id"], ["  person  ", "", "   ", "car"])
    assert set(store.get_object_detection_classes(cam["id"])) == {"person", "car"}


def test_class_confidence_upserts_rather_than_duplicating(store, cam):
    store.set_class_confidence_threshold(cam["id"], "person", 0.4)
    store.set_class_confidence_threshold(cam["id"], "person", 0.9)
    assert store.get_class_confidence(cam["id"], "person") == 0.9


# ===================================================================
# Zones
# ===================================================================

TRIANGLE = [[0.1, 0.1], [0.9, 0.1], [0.5, 0.9]]


def test_add_zone_round_trips(store, cam):
    zone = store.add_zone(cam["id"], "Driveway", TRIANGLE)
    zones = store.get_zones(cam["id"])
    assert len(zones) == 1
    assert zones[0]["name"] == "Driveway"
    assert zone["id"] == zones[0]["id"]


def test_zone_points_survive_round_trip(store, cam):
    store.add_zone(cam["id"], "Z", TRIANGLE)
    points = store.get_zones(cam["id"])[0]["points"]
    assert len(points) == 3
    for actual, expected in zip(points, TRIANGLE):
        assert pytest.approx(list(actual)) == expected


def test_zone_needs_at_least_three_points(store, cam):
    with pytest.raises(ValueError):
        store.add_zone(cam["id"], "Line", [[0.1, 0.1], [0.9, 0.9]])


@pytest.mark.parametrize("bad_point", [[1.5, 0.5], [-0.1, 0.5], [0.5, 2.0]])
def test_zone_points_must_be_normalised(store, cam, bad_point):
    """Points are 0.0-1.0 relative to the source frame, never pixels.
    A pixel value leaking in here is a bug worth catching loudly."""
    with pytest.raises(ValueError):
        store.add_zone(cam["id"], "Bad", [[0.1, 0.1], [0.9, 0.1], bad_point])


def test_zone_name_cannot_be_empty(store, cam):
    with pytest.raises(ValueError):
        store.add_zone(cam["id"], "   ", TRIANGLE)


def test_remove_zone(store, cam):
    zone = store.add_zone(cam["id"], "Z", TRIANGLE)
    store.remove_zone(cam["id"], zone["id"])
    assert store.get_zones(cam["id"]) == []


def test_zones_are_deleted_with_their_camera(store, cam):
    """Relies on PRAGMA foreign_keys = ON actually being set in
    _connect(). If someone removes that pragma, this test is the
    tripwire -- orphaned rows would otherwise accumulate silently."""
    store.add_zone(cam["id"], "Z", TRIANGLE)
    store.remove_camera(cam["id"])

    conn = store._connect()
    try:
        rows = conn.execute(
            "SELECT COUNT(*) FROM zones WHERE camera_id = ?", (cam["id"],)
        ).fetchone()[0]
    finally:
        conn.close()
    assert rows == 0


# ===================================================================
# Alert rules
# ===================================================================

def test_add_alert_rule_round_trips(store, cam):
    rule = store.add_alert_rule(
        cam["id"], "Night watch", "22:00", "06:00", "motion"
    )
    rules = store.get_alert_rules(cam["id"])
    assert len(rules) == 1
    assert rules[0]["name"] == "Night watch"
    assert rules[0]["id"] == rule["id"]


def test_alert_rule_classes_round_trip(store, cam):
    store.add_alert_rule(
        cam["id"], "People", "00:00", "23:59", "object_class", ["person", "car"]
    )
    rule = store.get_alert_rules(cam["id"])[0]
    assert set(rule["classes"]) == {"person", "car"}


@pytest.mark.parametrize("bad_time", ["25:00", "nonsense", "", "12:60", "1:2:3"])
def test_alert_rule_rejects_malformed_times(store, cam, bad_time):
    with pytest.raises(ValueError):
        store.add_alert_rule(cam["id"], "R", bad_time, "23:59", "motion")


def test_alert_rule_normalises_unpadded_times(store, cam):
    """"6:00" is accepted and stored as "06:00". Deliberate leniency --
    alert_matcher.parse_hhmm relies on the stored form being padded."""
    rule = store.add_alert_rule(cam["id"], "R", "6:00", "9:5", "motion")
    stored = store.get_alert_rules(cam["id"])[0]
    assert stored["start"] == "06:00"
    assert stored["end"] == "09:05"
    assert rule is not None


def test_midnight_crossing_window_is_allowed(store, cam):
    """start > end is legal and means the window wraps midnight --
    see alert_matcher.time_in_window."""
    rule = store.add_alert_rule(cam["id"], "Night", "22:00", "06:00", "motion")
    assert rule is not None


def test_remove_alert_rule(store, cam):
    rule = store.add_alert_rule(cam["id"], "R", "00:00", "23:59", "motion")
    store.remove_alert_rule(cam["id"], rule["id"])
    assert store.get_alert_rules(cam["id"]) == []


def test_update_alert_rule_changes_only_what_is_passed(store, cam):
    rule = store.add_alert_rule(cam["id"], "Old", "01:00", "02:00", "motion")
    updated = store.update_alert_rule(cam["id"], rule["id"], name="New")
    assert updated["name"] == "New"
    assert updated["start"] == "01:00"  # untouched
    assert updated["end"] == "02:00"


def test_alert_rules_are_deleted_with_their_camera(store, cam):
    store.add_alert_rule(cam["id"], "R", "00:00", "23:59", "motion")
    store.remove_camera(cam["id"])

    conn = store._connect()
    try:
        rows = conn.execute(
            "SELECT COUNT(*) FROM alert_rules WHERE camera_id = ?", (cam["id"],)
        ).fetchone()[0]
    finally:
        conn.close()
    assert rows == 0


# ===================================================================
# Isolation between cameras
# ===================================================================

def test_settings_do_not_leak_between_cameras(store):
    """Two cameras, one changed. The other must be untouched -- a
    missing WHERE camera_id would pass every single-camera test above
    and fail here."""
    a = store.add_camera("A", "rtsp://a")
    b = store.add_camera("B", "rtsp://b")

    store.set_motion_sensitivity(a["id"], "high")
    store.set_object_detection_classes(a["id"], ["giraffe"])
    store.add_zone(a["id"], "Za", TRIANGLE)

    fresh_b = store.get_camera(b["id"])
    assert fresh_b["motion_sensitivity"] != "high"
    assert "giraffe" not in fresh_b["object_detection_classes"]
    assert fresh_b["object_detection_classes"] == CameraStore.DEFAULT_DETECTION_CLASSES
    assert fresh_b["zones"] == []


# ===================================================================
# Defaults for a freshly added camera
# ===================================================================

def test_new_camera_defaults(cam):
    """Pins the out-of-the-box behaviour. These defaults decide what a
    user sees before touching any setting, so changing one is a product
    decision, not an implementation detail."""
    assert cam["motion_enabled"] is True
    assert cam["motion_sensitivity"] in CameraStore.VALID_SENSITIVITIES
    assert cam["object_detection_classes"] == CameraStore.DEFAULT_DETECTION_CLASSES
    assert cam["alert_rules"] == []
    assert cam["zones"] == []


def test_default_detection_classes_are_seeded_per_camera(store):
    """Each camera gets its own copy of the defaults -- not a shared row."""
    a = store.add_camera("A", "rtsp://a")
    b = store.add_camera("B", "rtsp://b")
    store.set_object_detection_classes(a["id"], [])
    assert store.get_object_detection_classes(a["id"]) == []
    assert store.get_object_detection_classes(b["id"]) == CameraStore.DEFAULT_DETECTION_CLASSES
