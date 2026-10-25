"""Provider-independent checks for explicit planning data, never inferred facts."""
from __future__ import annotations
import re


def clock_minutes(value):
    if not isinstance(value, str) or not re.fullmatch(r"(?:[01]?\d|2[0-3]):[0-5]\d", value.strip()):
        return None
    hour, minute = map(int, value.strip().split(':'))
    return hour * 60 + minute


def core_day_issues(day):
    """Check known durations and incoming transfers across consecutive activities.

    Unknown durations interrupt the computable chain. No default travel speed,
    route time, opening hour, or expense amount is invented here.
    """
    issues = []
    earliest_end = None
    last_clock = None
    for index, activity in enumerate(day.get('activities', [])):
        start = clock_minutes(activity.get('start_time') or activity.get('time'))
        if start is not None:
            if last_clock is not None and start < last_clock:
                issues.append({'code': 'activity_time_order', 'activity_index': index,
                               'message': f"Day {day.get('day', 1)} activity {index + 1}: clock times run backwards."})
            last_clock = start
        duration = activity.get('duration_minutes')
        transfer = activity.get('transfer_minutes', 0)
        buffer = activity.get('buffer_minutes', 0)
        incoming = sum(v for v in (transfer, buffer) if isinstance(v, int) and not isinstance(v, bool) and v >= 0)
        if start is not None and earliest_end is not None and start < earliest_end + incoming:
            issues.append({'code': 'activity_time_overlap', 'activity_index': index,
                           'message': f"Day {day.get('day', 1)} activity {index + 1}: start precedes completion of earlier activities plus transfer/buffer."})
        effective_start = start if start is not None else (earliest_end + incoming if earliest_end is not None else None)
        earliest_end = (effective_start + duration if effective_start is not None and
                        isinstance(duration, int) and not isinstance(duration, bool) and duration > 0 else None)
        if earliest_end is not None and earliest_end > 1440:
            issues.append({'code': 'activity_exceeds_day', 'activity_index': index,
                           'message': f"Day {day.get('day', 1)} activity {index + 1}: schedule crosses midnight; split or explicitly replan the day."})
        cost = str(activity.get('cost_estimate') or '')
        basis = activity.get('cost_basis')
        person = bool(re.search(r'\bper (?:person|adult)\b', cost, re.I))
        group = bool(re.search(r'\b(?:for (?:two|2) (?:adults|people)|per group)\b', cost, re.I))
        if (basis == 'per_person' and group) or (basis == 'per_group' and person):
            issues.append({'code': 'cost_basis_conflict', 'activity_index': index,
                           'message': f"Day {day.get('day', 1)} activity {index + 1}: cost text contradicts cost_basis; preserve the actual unit."})
    return issues
