"""Tests for core.alerts.alert_matcher.

This module is pure logic with no I/O and no threading -- exactly the
kind of thing that should be exhaustively tested, especially around
midnight-crossing windows. Run with:  pytest
"""

from datetime import time

import pytest

from core.alerts.alert_matcher import (
    active_motion_rules,
    active_object_class_rules,
    parse_hhmm,
    rule_time_matches,
    time_in_window,
)

# ----- parse_hhmm -----------------------------------------------------

def test_parse_hhmm_valid():
    assert parse_hhmm("06:30") == time(6, 30)
    assert parse_hhmm(" 23:59 ") == time(23, 59)


@pytest.mark.parametrize("bad", ["", "6", "6:30:00", "abc", None])
def test_parse_hhmm_rejects_malformed(bad):
    with pytest.raises(ValueError):
        parse_hhmm(bad)


# ----- time_in_window -------------------------------------------------

def test_same_day_window():
    start, end = time(6, 0), time(22, 0)
    assert time_in_window(time(6, 0), start, end)      # inclusive start
    assert time_in_window(time(12, 0), start, end)
    assert not time_in_window(time(22, 0), start, end)  # exclusive end
    assert not time_in_window(time(5, 59), start, end)


def test_window_crossing_midnight():
    start, end = time(22, 0), time(6, 0)
    assert time_in_window(time(23, 0), start, end)
    assert time_in_window(time(0, 0), start, end)
    assert time_in_window(time(5, 59), start, end)
    assert not time_in_window(time(6, 0), start, end)
    assert not time_in_window(time(12, 0), start, end)


def test_zero_width_window_matches_all_day():
    start = end = time(9, 0)
    assert time_in_window(time(9, 0), start, end)
    assert time_in_window(time(3, 0), start, end)


# ----- rule matching --------------------------------------------------

def _rule(**kw):
    base = {
        "id": "r1", "name": "test", "enabled": True,
        "start": "00:00", "end": "23:59",
        "trigger_type": "motion", "classes": [],
    }
    base.update(kw)
    return base


def test_disabled_rule_never_matches():
    assert not rule_time_matches(_rule(enabled=False), time(12, 0))


def test_malformed_rule_is_skipped_not_raised():
    assert not rule_time_matches(_rule(start="nonsense"), time(12, 0))


def test_active_motion_rules_ignores_object_class_rules():
    camera = {"alert_rules": [
        _rule(id="m", trigger_type="motion"),
        _rule(id="o", trigger_type="object_class", classes=["person"]),
    ]}
    ids = [r["id"] for r in active_motion_rules(camera, time(12, 0))]
    assert ids == ["m"]


def test_active_object_class_rules_filters_on_class():
    camera = {"alert_rules": [
        _rule(id="a", trigger_type="object_class", classes=["person"]),
        _rule(id="b", trigger_type="object_class", classes=["car"]),
    ]}
    ids = [r["id"] for r in active_object_class_rules(camera, "person", time(12, 0))]
    assert ids == ["a"]


def test_overlapping_rules_all_match():
    camera = {"alert_rules": [
        _rule(id="a", start="00:00", end="23:59"),
        _rule(id="b", start="10:00", end="14:00"),
    ]}
    ids = {r["id"] for r in active_motion_rules(camera, time(12, 0))}
    assert ids == {"a", "b"}


def test_camera_with_no_rules():
    assert active_motion_rules({}, time(12, 0)) == []
