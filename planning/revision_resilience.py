"""Deterministic, bounded recovery of model-proposed itinerary changes.

Scheduling repairs only move newly inserted stops into visible clock gaps.
No external opening hours, transport estimates or verified facts are invented.
"""
from __future__ import annotations

from copy import deepcopy
from planning.time_contract import activity_start_minute


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
    """Sort chronologically and repair a movable addition using existing timing facts.

    Do not silently shift previously accepted stops. Only an inserted/replaced
    activity (origin=None) is movable, and no more than three hours from its
    proposal. Explicit inbound transfer/buffer requirements are preserved.
    A 15-minute planning margin for distinct named areas is a provisional
    minimum, not a claim about actual travel time.
    """
    result = deepcopy(day)
    activities = result.get('activities', [])
    if not isinstance(activities, list) or len(activities) != len(origins):
        return result, origins
    indexed = [(deepcopy(a), origin) for a, origin in zip(activities, origins)]
    if sum(_interval(a) is not None for a, _ in indexed) < 2:
        return result, origins

    def needed(previous: dict, following: dict) -> int:
        transfer = following.get('transfer_minutes')
        buffer = following.get('buffer_minutes')
        explicit = (transfer if type(transfer) is int and transfer >= 0 else 0) + (buffer if type(buffer) is int and buffer >= 0 else 0)
        prior_area = str(previous.get('area') or '').casefold().strip()
        next_area = str(following.get('area') or '').casefold().strip()
        prior_venue = str(previous.get('place_name') or previous.get('description') or '').strip().casefold()
        next_venue = str(following.get('place_name') or following.get('description') or '').strip().casefold()
        distinct = bool(prior_venue and next_venue and prior_venue != next_venue)
        margin = 15 if distinct or (prior_area and next_area and prior_area != next_area) else 0
        return max(explicit, margin)

    fixed = [a for a, origin in indexed if origin is not None and _interval(a)]
    movable = [(a, origin) for a, origin in indexed if origin is None and _interval(a)]
    for activity, origin in movable:
        current = _interval(activity)
        if current is None:
            continue
        duration = current[1] - current[0]
        earliest = current[0]
        if type(not_before_minute) is int:
            earliest = max(earliest, not_before_minute + 1)
        occupied = sorted(fixed, key=lambda a: _interval(a)[0])

        def feasible(start: int) -> bool:
            if start + duration > 1440:
                return False
            trial = sorted(occupied + [{**activity, 'start_time': _clock(start), 'time': None}],
                           key=lambda a: _interval(a)[0])
            for left, right in zip(trial, trial[1:]):
                if _interval(left)[1] + needed(left, right) > _interval(right)[0]:
                    return False
            return True

        if not feasible(earliest):
            # Try just after previously booked stops with their own documented
            # transfer requirements. Do not use a fake opening-hours provider.
            candidates = {earliest}
            for stop in occupied:
                stop_interval = _interval(stop)
                if stop_interval:
                    candidates.add(max(earliest, stop_interval[1] + needed(stop, activity)))
            for candidate in sorted(candidates):
                if candidate - current[0] > 180:
                    break
                if feasible(candidate):
                    activity['start_time'] = _clock(candidate)
                    activity.pop('time', None)
                    activity.pop('transport_hint', None)
                    # A moved stop's previous route is stale; keep an explicitly
                    # supplied duration as a required minimum, not a verified ETA.
                    reminder = 'Recheck transfer and opening hours for the adjusted stop.'
                    activity['verification_needed'] = reminder
                    break
        fixed.append(activity)
    indexed.sort(key=lambda item: (
        activity_start_minute(item[0]) is None,
        activity_start_minute(item[0]) if activity_start_minute(item[0]) is not None else 1500,
        item[1] is None))
    result['activities'] = [item[0] for item in indexed]
    return result, [item[1] for item in indexed]


def provisional_schedule_issues(document: dict) -> list[dict]:
    """Return visible problems for recoverable drafts (never a success claim)."""
    from planning.validation_gate import _interval_issues
    issues = []
    for day in document.get('days', []):
        for issue in _interval_issues(day):
            issues.append(issue.as_dict())
    return issues[:20]
