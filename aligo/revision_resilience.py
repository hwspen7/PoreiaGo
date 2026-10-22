"""Deterministic, bounded recovery of model-proposed itinerary changes.

Scheduling repairs only move newly inserted stops into visible clock gaps.
No external opening hours, transport estimates or verified facts are invented.
"""
from __future__ import annotations

from copy import deepcopy
from .time_contract import activity_start_minute


def _interval(activity: dict):
    start = activity_start_minute(activity)
    duration = activity.get('duration_minutes')
    if start is None or type(duration) is not int or duration <= 0:
        return None
    return start, start + duration


def _clock(minutes: int) -> str:
    return f'{minutes // 60:02d}:{minutes % 60:02d}'


def reconcile_schedule(day: dict, origins: list[int | None], *,
                       not_before_minute: int | None = None) -> tuple[dict, list[int | None]]:
    """Chronologically order a day and relocate *new* overlapping stops only.

    Existing visits retain their explicit times. A different named area needs
    a small scheduling margin, which is not represented as a verified ETA.
    """
    result = deepcopy(day)
    activities = result.get('activities', [])
    if not isinstance(activities, list) or len(activities) != len(origins):
        return result, origins
    indexed = [(deepcopy(a), origin) for a, origin in zip(activities, origins)]
    if sum(_interval(a) is not None for a, _ in indexed) < 2:
        return result, origins
    fixed = [(a, _interval(a)) for a, origin in indexed if origin is not None and _interval(a)]
    for activity, origin in indexed:
        if origin is not None:
            continue
        current = _interval(activity)
        if current is None:
            continue
        duration = current[1] - current[0]
        occupied = sorted((interval[0], interval[1], a) for a, interval in fixed if interval)
        # Place a proposed visit no earlier than its requested clock time.
        earliest = current[0]
        if isinstance(not_before_minute, int):
            earliest = max(earliest, not_before_minute + 1)
        def fits(start: int) -> bool:
            if start + duration > 1440:
                return False
            for before, after, _ in occupied:
                if start < after and start + duration > before:
                    return False
            return True
        if not fits(earliest):
            boundaries = {earliest}
            boundaries.update(end for _, end, _ in occupied if end >= earliest)
            for candidate in sorted(boundaries):
                # Do not treat a zero-minute transfer as a verified connection.
                candidate = max(candidate, earliest)
                if candidate > earliest:
                    candidate += 15
                # Large displacements are itinerary design decisions for the model.
                if candidate - current[0] > 180:
                    continue
                if fits(candidate):
                    activity['start_time'] = _clock(candidate)
                    activity.pop('time', None)
                    activity.pop('transport_hint', None)
                    activity.pop('transfer_minutes', None)
                    note = 'Confirm the transfer time and opening hours for this adjusted stop.'
                    if not activity.get('verification_needed'):
                        activity['verification_needed'] = note
                    break
        fixed.append((activity, _interval(activity)))
    indexed.sort(key=lambda item: (
        activity_start_minute(item[0]) is None,
        activity_start_minute(item[0]) if activity_start_minute(item[0]) is not None else 1500,
        item[1] is None))
    result['activities'] = [item[0] for item in indexed]
    return result, [item[1] for item in indexed]


def provisional_schedule_issues(document: dict) -> list[dict]:
    """Return visible problems for recoverable drafts (never a success claim)."""
    from .validation_gate import _interval_issues
    issues = []
    for day in document.get('days', []):
        for issue in _interval_issues(day):
            issues.append(issue.as_dict())
    return issues[:20]
