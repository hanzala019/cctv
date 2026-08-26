"""
alert_matcher.py

Phase 5: pure rule-matching logic for time-of-day alerting. Deliberately
has zero I/O and zero threading -- just functions of (rule dict, time)
and (camera dict, time) -- so it's the one piece of Phase 5 that's
cheap to unit test exhaustively, per the roadmap's own suggestion
("this is exactly the kind of logic that should have unit tests --
lots of edge cases around midnight").

Rule shape (see camera_store.py's module docstring for the canonical
version):
    {
        "id": "...",
        "name": "...",
        "enabled": True/False,
        "start": "HH:MM",
        "end": "HH:MM",
        "trigger_type": "motion" | "object_class",
        "classes": [...],   # only meaningful for "object_class"
    }

Trigger-type split
-------------------
Two separate lookup functions instead of one generic "does this rule
match this event" function, because the two trigger types pull their
match signal from different places in the running app:

    - motion rules match against a single camera-wide boolean the
      caller (alert_manager.py) computes as
      `result.motion or any(result.zones.values())` -- see that
      module's docstring for why both halves are needed once a camera
      has zones with detection_enabled (Phase 3's zone-priority rule
      makes the whole-frame bool permanently False in that case).
    - object_class rules match against a specific class name (the
      most recent ObjectDetectionManager event's class_name), so the
      class filter has to happen here, not just the time window.

Multiple rules can legitimately match at once (e.g. overlapping
windows) -- both lookup functions return *all* matches, not just the
first. alert_manager.py fires one alert lifecycle per matched rule
independently, per the Phase 5 design discussion.
"""

from datetime import time as _time


def parse_hhmm(value):
    """'HH:MM' -> datetime.time. Raises ValueError on anything malformed
    -- callers in camera_store.py validate at write time, but this
    stays defensive since rule dicts loaded from disk could in theory
    be hand-edited or come from an older/different format."""
    parts = (value or "").strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"Expected HH:MM, got {value!r}")
    h, m = int(parts[0]), int(parts[1])
    return _time(hour=h, minute=m)


def time_in_window(now_time, start_time, end_time):
    """True if now_time falls in [start_time, end_time), handling the
    case where the window crosses midnight (end_time < start_time).

    start_time == end_time is treated as "matches all 24 hours" --
    the least surprising interpretation of a zero-width window (the
    alternative, matching nothing, would make a rule silently useless
    with no error anywhere), rather than a special case callers need
    to avoid.
    """
    if start_time == end_time:
        return True
    if start_time < end_time:
        # Normal same-day window, e.g. 06:00-22:00.
        return start_time <= now_time < end_time
    # Wraps midnight, e.g. 22:00-06:00: "now" is in-window if it's
    # after start OR before end -- NOT a simple start <= now < end,
    # which would incorrectly exclude everything (this is the classic
    # bug the roadmap flagged explicitly).
    return now_time >= start_time or now_time < end_time


def rule_time_matches(rule, now_time):
    """enabled + time-window check only -- no trigger_type/class logic.
    Shared by both lookup functions below."""
    if not rule.get("enabled", True):
        return False
    try:
        start_time = parse_hhmm(rule.get("start", ""))
        end_time = parse_hhmm(rule.get("end", ""))
    except ValueError:
        return False  # malformed rule -- skip rather than crash
    return time_in_window(now_time, start_time, end_time)


def active_motion_rules(camera, now_time):
    """All enabled motion-trigger rules on this camera whose time
    window currently matches. Caller is responsible for having already
    determined that motion is actually happening right now (this
    function only answers "which rules would apply if it is")."""
    return [
        rule for rule in camera.get("alert_rules", [])
        if rule.get("trigger_type") == "motion" and rule_time_matches(rule, now_time)
    ]


def active_object_class_rules(camera, class_name, now_time):
    """All enabled object_class-trigger rules on this camera whose
    time window currently matches AND whose class allowlist includes
    class_name."""
    return [
        rule for rule in camera.get("alert_rules", [])
        if rule.get("trigger_type") == "object_class"
        and class_name in rule.get("classes", [])
        and rule_time_matches(rule, now_time)
    ]
