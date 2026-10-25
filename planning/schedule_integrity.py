"""Conservative, source-independent schedule checks for complete itinerary writes.

A small planning handoff is not a real travel-time estimate. This module only
rejects the demonstrably impossible case of consecutively timed distinct stops
with no useful handoff time. Unavailable clocks remain explicitly unverified.
"""
from __future__ import annotations

from planning.time_contract import activity_start_minute


def _physical_stop(activity: dict) -> bool:
    return activity.get('activity_type') not in {'transport', 'rest', 'lodging'}


def minimum_handoff_minutes(left: dict, right: dict) -> int:
    """Planning-only floor for separate physical activities; not a route ETA."""
    if not (_physical_stop(left) and _physical_stop(right)):
        return 0
    left_name = str(left.get('place_name') or '').strip().casefold()
    right_name = str(right.get('place_name') or '').strip().casefold()
    if left_name and right_name:
        return 0 if left_name == right_name else 15
    # Missing place identity cannot establish a distinct physical stop.
    # Avoid claiming a transfer duration from an unspecified location.
    if left_name or right_name:
        return 0
    if not left_name and not right_name:
        left_area = str(left.get('area') or '').strip().casefold()
        right_area = str(right.get('area') or '').strip().casefold()
        if left_area and right_area and left_area == right_area:
            # Different attractions in the same neighborhood can be adjacent,
            # but their handoff must not be certified as instantaneous.
            return 10
    return 0


def visible_handoff_issues(day: dict) -> list[tuple[int, str]]:
    """Check that consecutive known clocks provide a nonzero physical handoff.

    Existing explicit transfer and buffer requirements are checked separately
    by the canonical schedule invariants. Missing timing is not fabricated.
    """
    activities = day.get('activities') or []
    issues = []
    for idx in range(1, len(activities)):
        left, right = activities[idx - 1], activities[idx]
        left_start = activity_start_minute(left)
        right_start = activity_start_minute(right)
        left_length = left.get('duration_minutes')
        if (left_start is None or right_start is None or
                type(left_length) is not int or left_length <= 0):
            continue
        minimum = minimum_handoff_minutes(left, right)
        if minimum <= 0:
            continue
        available = right_start - left_start - left_length
        if 0 <= available < minimum:
            issues.append((idx + 1,
                           f'Day {day.get("day")}: activities {idx} and {idx + 1} '
                           f'allow only {available} minutes for a change of stop. '
                           f'At least {minimum} minutes of provisional handoff '
                           'is needed before this itinerary can be accepted. '
                           'Actual transit time still requires verification.'))
    return issues
