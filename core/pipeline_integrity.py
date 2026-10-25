"""Deterministic, source-independent guardrails for assembled travel plans.

These checks detect contradictions visible in the itinerary itself. They do not
pretend to verify real transit schedules, accommodation rules, or admission fees.
"""
from __future__ import annotations

import re
from typing import Any

_CLOCK = re.compile(r'^(?:[01]\d|2[0-3]):[0-5]\d$')
_MOVEMENT = re.compile(
    r'\b(?:travel|transfer|train|rail|bus|coach|flight|fly|ferry|drive|ride|'
    r'check[ -]?out|arriv(?:al|e)|depart(?:ure)?|luggage|bag(?:gage)?|'
    r'check[ -]?in|taxi|metro|subway)\b', re.I)
# Match an explicit airport name (case insensitive) or an uppercase IATA-like
# code (case sensitive). Do NOT compile the whole expression with IGNORECASE:
# doing so classified the article "the" in "via the train" as an airport code.
_AIRPORT_ORIGIN = re.compile(
    r'\b(?i:from|via)\s+(?:(?i:the)\s+)?'
    r'(?:[A-Z]{3}\b|(?i:(?:(?!to\b|toward\b|via\b|for\b)[\w-]+\s+){0,6}airport\b))')
# In a same-base excursion, a new lodging claim in the visited city is suspect.
# A real hotel move is determined by the overnight base, never by the excursion city.
_DAYTRIP_LODGING_CLAIM = re.compile(
    r'\b(?:drop\s+off|leave|store|deliver|forward)\s+'
    r'(?:your\s+|the\s+|our\s+)?(?:luggage|bags?|baggage)\s+'
    r'(?:at|to)\s+(?:your\s+|the\s+|a\s+)?'
    r'(?:accommodation|hotel|lodging)\b|'
    r'\bcheck\s*[- ]?in\s+(?:at\s+|to\s+)?'
    r'(?:your\s+|the\s+|a\s+)?(?:accommodation|hotel|lodging)\b', re.I)
_INTERNAL_REASON = re.compile(
    r'\b(?:aligns? with|fits|matches|supports)\s+(?:the\s+)?'
    r'[^.!?]{0,100}\b(?:theme|blueprint|anchor|allocation)\b|'
    r'\b(?:primary|core|major|second)\s+anchor\b|'
    r'\b(?:itinerary anchor|allocated day|assigned theme)\b', re.I)


def _minute(value: Any) -> int | None:
    """Parse a clock in a structured start or a human-readable time label.

    A bare daypart has no exact time and is deliberately not guessed.  A
    12-hour clock with an AM/PM marker and an embedded 24-hour clock are safe
    to interpret, provided an explicit minute value is present.
    """
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    if _CLOCK.fullmatch(text):
        hours, minutes = map(int, text.split(':'))
        return hours * 60 + minutes
    match = re.search(r'(?<!\d)(\d{1,2}):([0-5]\d)\s*(am|pm)?(?!\d)', text, re.I)
    if not match:
        return None
    hours, minutes = int(match.group(1)), int(match.group(2))
    marker = (match.group(3) or '').casefold()
    if marker:
        if not 1 <= hours <= 12:
            return None
        hours = (hours % 12) + (12 if marker == 'pm' else 0)
    elif hours > 23:
        return None
    return hours * 60 + minutes


def _timed_activity(activity: dict) -> tuple[int, int] | None:
    start = _minute(activity.get('start_time') or activity.get('time'))
    duration = activity.get('duration_minutes')
    if (start is None or not isinstance(duration, int)
            or isinstance(duration, bool) or duration <= 0):
        return None
    return start, start + duration


def schedule_invariants(day: dict) -> list[str]:
    """Validate all interval overlaps and each immediate inbound transfer.

    Missing clock/duration data must not silently certify an explicitly
    requested transfer buffer as feasible.  Such cases are reported as
    unverified constraints, not falsely described as known overlaps.
    """
    activities = day.get('activities') or []
    intervals: list[tuple[int, int, int]] = []
    issues: list[str] = []
    for index, item in enumerate(activities):
        if not isinstance(item, dict):
            continue
        timed = _timed_activity(item)
        # The incoming leg and the safety margin are independent constraints.
        # They must both fit between the previous activity's end and this start.
        transfer = item.get('transfer_minutes', 0)
        buffer = item.get('buffer_minutes', 0)
        if index > 0 and any(
            isinstance(value, int) and not isinstance(value, bool) and value > 0
            for value in (transfer, buffer)
        ):
            # Malformed values cannot establish a valid required interval.
            if (isinstance(transfer, bool) or not isinstance(transfer, int)
                    or isinstance(buffer, bool) or not isinstance(buffer, int)
                    or transfer < 0 or buffer < 0):
                issues.append(
                    f"Day {day.get('day')}: incoming transfer/buffer before "
                    f"activity {index + 1} has invalid minute values.")
            else:
                required = transfer + buffer
                previous = activities[index - 1]
                previous_timed = _timed_activity(previous) if isinstance(previous, dict) else None
                if timed is None or previous_timed is None:
                    issues.append(
                        f"Day {day.get('day')}: cannot verify the incoming transfer/buffer "
                        f"of {required} minutes ({transfer} transfer + {buffer} buffer) "
                        f"before activity {index + 1}; provide exact start times and "
                        'durations for adjacent stops.')
                elif timed[0] < previous_timed[1] + required:
                    available = timed[0] - previous_timed[1]
                    issues.append(
                        f"Day {day.get('day')}: incoming transfer/buffer of {required} "
                        f"minutes ({transfer} transfer + {buffer} buffer) before "
                        f"activity {index + 1} is not accommodated "
                        f"({available} minutes available, "
                        f"{required - available} minutes short).")
        if timed is None:
            continue
        start, end = timed
        for earlier_start, earlier_end, earlier_index in intervals:
            if start < earlier_end and end > earlier_start:
                issues.append(
                    f"Day {day.get('day')}: timed activities {earlier_index + 1} and "
                    f"{index + 1} overlap. Reschedule the activities, preserving "
                    'meal periods and travel time.')
        intervals.append((start, end, index))
    return issues


def transition_invariants(day: dict, *, previous_day: dict | None,
                          previous_allocation: dict | None,
                          allocation: dict) -> list[str]:
    """Require a visible origin and transfer before sightseeing after a base change."""
    if not previous_allocation or not previous_day:
        return []
    previous_base = str(previous_allocation.get('overnight') or '').strip().casefold()
    destination_base = str(allocation.get('overnight') or '').strip().casefold()
    previous_city = str(previous_allocation.get('city') or '').strip().casefold()
    current_city = str(allocation.get('city') or '').strip().casefold()
    if not previous_city or not current_city:
        return []
    hotel_changed = bool(previous_base and destination_base and
                         previous_base != destination_base)
    # Visiting another city while sleeping at the same base is a day trip.
    # The FOLLOWING morning starts at that overnight base, not at yesterday's
    # excursion city. In particular Kyoto -> Nara -> Kyoto is not two moves.
    day_trip_departure = (previous_city != current_city and
                          previous_base == destination_base and
                          current_city != destination_base)
    if not hotel_changed and not day_trip_departure:
        return []
    items = day.get('activities') or []
    if not items:
        return []
    first = items[0]
    first_text = ' '.join(str(first.get(field) or '') for field in
                          ('description', 'transport_hint', 'area'))
    issues: list[str] = []
    explicit_start = _minute(first.get('start_time') or first.get('time')) is not None
    if explicit_start and not _MOVEMENT.search(first_text):
        detail = ('show checkout, luggage and the journey' if hotel_changed else
                  'show the journey from the overnight base')
        issues.append(
            f"Day {day.get('day')}: the previous overnight base was "
            f"{previous_allocation.get('overnight')}; {detail} "
            f"to {allocation.get('city')} BEFORE the first attraction.")
    # A local leg inside the NEW city must not silently substitute for the
    # transfer from yesterday's hotel base (e.g., Osaka Station -> Sakai when
    # yesterday's lodging is in Kyoto).
    from_new_city = re.search(
        r'\bfrom\s+(?:the\s+)?' + re.escape(current_city) + r'\b',
        first_text, re.I)
    if (explicit_start and from_new_city and previous_city not in first_text.casefold()):
        issues.append(
            f"Day {day.get('day')}: route starts in {allocation.get('city')} "
            f"but the previous overnight base was {previous_allocation.get('overnight')}. "
            'Include the preceding intercity journey and luggage handling.')
    if _AIRPORT_ORIGIN.search(first_text) and not re.search(
            r'\b(?:airport|airfield)\b',
            ' '.join(str(previous_day.get(field) or '') for field in
                     ('overnight_base', 'base_city')),
            re.I):
        issues.append(
            f"Day {day.get('day')}: the trip is continuing from "
            f"{previous_allocation.get('overnight')}, not a fresh airport arrival. "
            'Correct the origin and transport route.')
    # Day trips may involve travelling to a different city, but must not
    # silently create an additional hotel stay in that city. A documented
    # stop at the existing base's hotel is not an error.
    is_day_trip = day_trip_departure
    if is_day_trip and _DAYTRIP_LODGING_CLAIM.search(first_text):
        overnight_mentioned = bool(re.search(
            r'\b' + re.escape(previous_base) + r'\s+(?:hotel|accommodation|lodging)\b',
            first_text, re.I))
        if not overnight_mentioned:
            issues.append(
                f"Day {day.get('day')}: this is a day trip to "
                f"{allocation.get('city')} with overnight base "
                f"{allocation.get('overnight')}. Do not invent lodging "
                'or a luggage drop-off in the visited city.')
    return issues


def _canon(text: str) -> str:
    return ' '.join(re.findall(r'[^\W_]+', text.casefold()))


def stale_repair_metadata(original: dict, revised: dict,
                          *, removed_venues: list[str] | None = None) -> list[str]:
    """Reject a one-activity edit when day-level copy still mentions its old stop.

    Exact removed names and strong, explicitly named site types are checked;
    generic neighborhood words and transport nodes can legitimately recur.
    """
    narrative = ' '.join(str(revised.get(key) or '') for key in
                         ('title', 'logistics', 'weather_alternative', 'budget_note', 'notes'))
    current = ' '.join(str(a.get('description') or '') + ' ' +
                       str(a.get('place_name') or '') for a in
                       revised.get('activities') or [])
    name_keys = {_canon(name) for name in (removed_venues or []) if len(_canon(name)) >= 8}
    for name in name_keys:
        if name in _canon(narrative) and name not in _canon(current):
            return [f'Day-level copy still refers to removed venue {name!r}.']
    # A removed market, museum, shrine, or workshop may leave misleading generic
    # references in the title/logistics even when no exact venue name survived.
    old_items = original.get('activities') or []
    new_items = revised.get('activities') or []
    for category in ('market', 'museum', 'workshop', 'brewery'):
        old_had = any(re.search(rf'\b{category}\b', str(a.get('description') or ''), re.I)
                      for a in old_items)
        new_has = any(re.search(rf'\b{category}\b', str(a.get('description') or ''), re.I)
                      for a in new_items)
        if old_had and not new_has and re.search(rf'\b{category}\b', narrative, re.I):
            return [f'Day-level copy retains removed {category} experience.']
    return []


def internal_reason_issue(day: dict) -> list[str]:
    return [f"Day {day.get('day')} activity {index + 1}: rewrite Why in traveler terms."
            for index, activity in enumerate(day.get('activities') or [])
            if _INTERNAL_REASON.search(str(activity.get('why_this_fits') or ''))]


def named_weather_venues(text: str) -> list[str]:
    """Extract recognizable venue names; never infer a venue from generic prose."""
    pattern = re.compile(
        r'\b((?:[A-Z][A-Za-z-]+\s+){0,3}'
        r'(?:Museum|Gallery|Aquarium|Castle|Palace|Temple|Shrine)'
        r'(?:\s+(?:of|and|the|[A-Z][A-Za-z-]+)){0,5})\b')
    names = []
    for match in pattern.finditer(str(text or '')):
        name = match.group(1).strip()
        if len(_canon(name)) >= 12 and _canon(name) not in {_canon(n) for n in names}:
            names.append(name)
    return names
