"""Canonical clock handling for persisted and model-authored itinerary records."""
from __future__ import annotations

from core.pipeline_integrity import _minute


def activity_start_minute(activity: dict) -> int | None:
    """Legacy `time` and modern `start_time` use the same exact clock parser."""
    primary = _minute(activity.get('start_time'))
    alternate = _minute(activity.get('time'))
    if primary is not None and alternate is not None and primary != alternate:
        from planning.trip_state import TripValidationError
        raise TripValidationError('ACTIVITY_TIME_FIELDS_DISAGREE')
    return primary if primary is not None else alternate


def activity_start(activity: dict) -> str | None:
    minute = activity_start_minute(activity)
    return f'{minute // 60:02d}:{minute % 60:02d}' if minute is not None else None


def normalized_clock_activity(activity: dict) -> dict:
    """Return a defensive copy with a canonical start, preserving legacy display."""
    result = dict(activity)
    start = activity_start(result)
    if start is not None:
        result['start_time'] = start
    return result
