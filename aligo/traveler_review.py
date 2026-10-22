"""Conservative traveler-facing quality review over validated itinerary data.

This module deliberately does not pretend to geocode places or verify prices.
It reports observable conflicts and identifies facts needing independent sources.
"""
from __future__ import annotations

from collections import Counter
import re
from typing import Any

from .place_evidence import match_place_candidates, analyze_evidence_routes


_DAYPARTS = {
    'early morning': (300, 600), 'morning': (360, 720),
    'late morning': (600, 780), 'lunch': (660, 870),
    'early afternoon': (720, 900), 'afternoon': (720, 1080),
    'late afternoon': (900, 1170), 'evening': (1020, 1440),
    'dinner': (1020, 1440), 'night': (1140, 1440),
    'mañana': (360, 720), 'mediodía': (660, 870),
    'tarde': (720, 1080), 'noche': (1020, 1440),
}
_MEAL_WORDS = re.compile(
    r'\b(lunch|dinner|breakfast|brunch|almuerzo|comida|cena|desayuno)\b', re.I)
_THEME_TEMPLATE = re.compile(
    r'\b(?:aligns? with|fits|matches|supports)\s+(?:the\s+)?[\u201c\u201d\'\"]?'
    r'(?:[^.!?]{0,100}?)\b(?:theme|blueprint|anchor|allocation)\b', re.I)
_ANCHOR_TEMPLATE = re.compile(
    r'\b(?:primary|core|main|second|major)\s+anchor\b|'
    r'\b(?:fulfills? the (?:primary )?anchor|assigned (?:day )?theme)\b', re.I)
_PRICE_NOT_VERIFIED = re.compile(r'\bunverified\b', re.I)
_TWO_PERSON = re.compile(r'\b(?:two|2)\s+(?:adults?|people|travelers?)\b', re.I)
_PER_PERSON = re.compile(r'\b(?:per person|per adult|each traveler)\b', re.I)
_NAMED_BACKUP = re.compile(
    r'\b((?:[A-Z][A-Za-z-]+\s+){0,3}'
    r'(?:Museum|Gallery|Aquarium|Castle|Palace|Temple|Shrine)'
    r'(?:\s+(?:of|and|the|[A-Z][A-Za-z-]+)){0,5})\b')



def _daypart_minutes(value: str) -> tuple[int, int] | None:
    text = value.strip().casefold()
    if re.fullmatch(r'(?:[01]?\d|2[0-3]):[0-5]\d', text):
        hour, minute = map(int, text.split(':'))
        point = hour * 60 + minute
        return (point, point)
    for part in sorted(_DAYPARTS, key=len, reverse=True):
        if part == text:
            return _DAYPARTS[part]
    return None


def _canonical(value: str) -> str:
    return ' '.join(re.findall(r'[^\W_]+', value.casefold(), re.UNICODE))


def _meal_signature(description: str) -> str:
    """Only capture named dishes after a sufficiently explicit lexical cue."""
    text = description.strip()
    match = re.search(r'\bspecifically\s+([\w-]+(?:\s+[\w-]+){0,2})', text, re.I)
    if match:
        return _canonical(match.group(1))
    match = re.search(r'\b(?:try|sample|tasting of)\s+([\w-]+(?:\s+[\w-]+){0,2})', text, re.I)
    return _canonical(match.group(1)) if match else ''


def _issue(severity: str, code: str, message: str, *, day: int | None = None,
           activity: int | None = None, field: str | None = None) -> dict[str, Any]:
    issue: dict[str, Any] = {'severity': severity, 'code': code, 'message': message}
    if day is not None:
        issue['day'] = day
    if activity is not None:
        issue['activity_index'] = activity
    if field is not None:
        issue['field'] = field
    return issue


def review_traveler_quality(itinerary: dict, *,
                            evidence: list[dict] | None = None,
                            request: str = '') -> dict[str, Any]:
    """Detect text/schedule contradictions without claiming external verification.

    All findings are advisory until the relevant independent location/transport
    evidence is actually available. The original itinerary is never mutated.
    """
    rows = itinerary.get('days', []) if isinstance(itinerary, dict) else []
    if not isinstance(rows, list):
        rows = []
    issues: list[dict[str, Any]] = []
    backup_owners: dict[str, int] = {}
    named_backup_owners: dict[str, int] = {}
    dish_owners: dict[str, int] = {}
    per_person_seen = False
    group_basis_seen = False
    cost_count = 0
    evidence_count = 0
    previous_overnight = ''
    for ordinal, day in enumerate(rows, 1):
        if not isinstance(day, dict):
            continue
        number = day.get('day', ordinal)
        if not isinstance(number, int):
            number = ordinal
        backup = str(day.get('weather_alternative') or '').strip()
        normalized_backup = _canonical(backup)
        if normalized_backup and len(normalized_backup) > 20:
            prior = backup_owners.setdefault(normalized_backup, number)
            if prior != number:
                issues.append(_issue('warning', 'repeated_weather_backup',
                                     f'Day {number} repeats Day {prior} weather alternative; '
                                     'check whether it is locally practical.', day=number,
                                     field='weather_alternative'))
        # Two different sentences may recommend the same named indoor venue.
        # Track that named venue independently of the full backup wording.
        for match in _NAMED_BACKUP.finditer(backup):
            venue = _canonical(match.group(1))
            if len(venue) < 12:
                continue
            other = named_backup_owners.setdefault(venue, number)
            if other != number and normalized_backup != _canonical(
                    str((rows[other - 1] or {}).get('weather_alternative') or '')):
                issues.append(_issue('warning', 'repeated_weather_venue',
                                     f'Day {number} reuses the Day {other} rain venue '
                                     f'{match.group(1)!r}; consider a nearby alternative.',
                                     day=number, field='weather_alternative'))
        overnight = str(day.get('overnight_base') or '').strip()
        if (previous_overnight and overnight and previous_overnight.casefold() != overnight.casefold()
                and not str(day.get('intercity_transfer') or '').strip()):
            issues.append(_issue('error', 'missing_intercity_transfer',
                                 f'Day {number} changes overnight base without an explicit '
                                 'intercity transfer; account for luggage and travel time.',
                                 day=number, field='intercity_transfer'))
        if overnight:
            previous_overnight = overnight
        budget_note = str(day.get('budget_note') or '')
        group_basis_seen |= bool(_TWO_PERSON.search(budget_note))
        per_person_seen |= bool(_PER_PERSON.search(budget_note))
        activities = day.get('activities', [])
        if not isinstance(activities, list):
            continue
        previous_clock: tuple[int, int] | None = None
        previous_duration = 0
        for index, activity in enumerate(activities):
            if not isinstance(activity, dict):
                continue
            description = str(activity.get('description') or '')
            why = str(activity.get('why_this_fits') or '')
            time_label = str(activity.get('start_time') or activity.get('time') or '')
            cost = str(activity.get('cost_estimate') or '')
            if _THEME_TEMPLATE.search(why) or _ANCHOR_TEMPLATE.search(why):
                issues.append(_issue('warning', 'internal_planning_language',
                                     f'Day {number} activity {index + 1}: replace internal planning '
                                     'terminology with a traveler-specific reason.', day=number,
                                     activity=index, field='why_this_fits'))
            meal = _MEAL_WORDS.search(description)
            parsed = _daypart_minutes(time_label)
            if parsed and parsed[0] == parsed[1]:
                if previous_clock and previous_clock[0] == previous_clock[1] and (
                        previous_clock[0] + previous_duration > parsed[0]):
                    issues.append(_issue('error', 'activity_time_overlap',
                                         f'Day {number} activity {index + 1} starts before the '
                                         'previous activity finishes; allow travel and dwell time.',
                                         day=number, activity=index, field='start_time'))
                previous_clock = parsed
                previous_duration = activity.get('duration_minutes', 0)
                if not isinstance(previous_duration, int) or isinstance(previous_duration, bool):
                    previous_duration = 0
            if meal and parsed:
                label = meal.group(1).lower()
                expected = ('lunch' if label in ('lunch', 'brunch', 'almuerzo', 'comida')
                            else 'breakfast' if label in ('breakfast', 'desayuno')
                            else 'dinner')
                window = {'breakfast': (360, 660), 'lunch': (660, 900),
                          'dinner': (1020, 1440)}[expected]
                if (parsed[1] < window[0] or parsed[0] > window[1]
                        or (expected == 'lunch' and time_label.strip().casefold()
                            in {'afternoon', 'late afternoon', 'evening', 'night'})):

                    issues.append(_issue('error', 'meal_time_mismatch',
                                         f'Day {number} activity {index + 1}: {expected} '
                                         f'is labeled {time_label!r}.', day=number,
                                         activity=index, field='time'))
            if meal:
                dish = _meal_signature(description)
                if dish and len(dish) >= 6:
                    prior = dish_owners.setdefault(dish, number)
                    if prior != number:
                        issues.append(_issue('warning', 'repeated_dish',
                                             f'Days {prior} and {number} repeat the same '
                                             f'named dish ({dish}); justify or diversify.',
                                             day=number, activity=index))
            if cost:
                cost_count += 1
                if _PRICE_NOT_VERIFIED.search(cost) and len(_PRICE_NOT_VERIFIED.findall(cost)) > 1:
                    issues.append(_issue('warning', 'duplicate_price_caveat',
                                         f'Day {number} activity {index + 1} repeats '
                                         'the unverified-price label.', day=number,
                                         activity=index, field='cost_estimate'))
                # A model-supplied URL/"verified" label is NOT independent
                # verification. Trusted provider attestations must be added by
                # a separate source-checking integration before counting it.
                if _PER_PERSON.search(cost):
                    per_person_seen = True
                if _TWO_PERSON.search(cost):
                    group_basis_seen = True
        if (re.search(r'\b(?:food|regional meals?|dining)\b', request, re.I)
                and number > 1 and number < len(rows) and
                not any(re.search(r'\b(?:dinner|evening meal|supper|cena)\b',
                                  str(a.get('description') or ''), re.I)
                        or a.get('meal_type') == 'dinner' for a in activities)
                and not re.search(r'\b(?:dinner|supper|evening meal)\b',
                                  str(day.get('notes') or ''), re.I)):
            issues.append(_issue('warning', 'no_dinner_option',
                                 f'Day {number} does not include a dinner option; '
                                 'check whether this is an intentional free evening.', day=number))
        if (number == len(rows) and overnight.casefold() in ('', 'n/a', 'none', 'departure') and
                any('airport' in str(a.get('description') or '').casefold()
                    for a in activities) and
                not re.search(r'\b(?:flight departs?|flight at|flight time|'
                              r'flight departure time)\s*(?:at\s*)?\d{1,2}:\d{2}',
                              request, re.I)):
            severity = ('warning' if re.search(r'\b(?:flight|airport|airfare)\b',
                                                request, re.I) else 'info')
            issues.append(_issue(severity, 'departure_flight_unspecified',
                                 'Airport transfer times are provisional until the '
                                 'return flight and check-in deadline are known.', day=number))
        if number > 1 and not day.get('intercity_transfer'):
            logistics = str(day.get('logistics') or '')
            if 'from the station' in logistics.casefold() and day.get('overnight_base'):
                issues.append(_issue('info', 'verify_daily_origin',
                                     f'Day {number}: check whether the day should start '
                                     'from the hotel rather than the rail station.',
                                     day=number, field='logistics'))
    if per_person_seen and group_basis_seen:
        issues.append(_issue('warning', 'mixed_cost_basis',
                             'The itinerary mixes per-person and group costs; '
                             'separate or normalize units before comparing totals.'))
    if cost_count and evidence_count < cost_count:
        issues.append(_issue('info', 'price_verification_pending',
                             f'{cost_count - evidence_count} of {cost_count} cost estimates '
                             'lack an attached verified source. Treat them as estimates.'))
    grounding = match_place_candidates(itinerary, evidence)
    route_evidence = analyze_evidence_routes(itinerary, evidence)
    evidenced_issues = {(row['day'], row['activity_index'])
                        for row in route_evidence['warnings']}
    for day in rows:
        if not isinstance(day, dict):
            continue
        activities = day.get('activities') or []
        for index in range(1, len(activities)):
            before, after = activities[index - 1], activities[index]
            if not isinstance(before, dict) or not isinstance(after, dict):
                continue
            if (day.get('day'), index) in evidenced_issues:
                continue
            hint = str(after.get('transport_hint') or '').casefold()
            # Warn on an explicit transfer with no clock allowance, even if
            # routing evidence is unavailable. Never infer a journey length.
            if not re.search(r'\b(?:walk|walkable|walking|on foot|rideshare|taxi|bus|train|subway|transit)\b', hint):
                continue
            before_name = _canonical(str(before.get('place_name') or ''))
            after_name = _canonical(str(after.get('place_name') or ''))
            if before_name and before_name == after_name:
                continue
            before_clock = _daypart_minutes(str(before.get('start_time') or before.get('time') or ''))
            after_clock = _daypart_minutes(str(after.get('start_time') or after.get('time') or ''))
            duration = before.get('duration_minutes')
            if (not before_clock or not after_clock or
                    before_clock[0] != before_clock[1] or
                    after_clock[0] != after_clock[1] or
                    not isinstance(duration, int) or isinstance(duration, bool)):
                continue
            if after_clock[0] - before_clock[0] - duration <= 0:
                issues.append(_issue(
                    'warning', 'unbudgeted_transfer',
                    f"Day {day.get('day')} activities {index} and {index + 1}: "
                    'no time was reserved for the explicitly described transfer. '
                    'Verify whether the stops are co-located or allow travel time.',
                    day=day.get('day'), activity=index, field='transport_hint'))
    for warning in route_evidence['warnings']:
        issues.append(_issue('warning', 'walking_transfer_infeasible_lower_bound',
                             warning['message'], day=warning['day'],
                             activity=warning['activity_index'], field='transport_hint'))
    # A bounded map candidate can contradict an asserted single-area cluster.
    # This is an advisory comparison of provider metadata, not certified geography.
    day_by_number = {day.get('day'): day for day in rows if isinstance(day, dict)}
    cluster_noted = set()
    for matched in grounding['matches']:
        number = matched['day']
        day = day_by_number.get(number) or {}
        cluster = _canonical(str(day.get('area_cluster') or ''))
        area = _canonical(str(matched['candidate'].get('area') or
                              matched['candidate'].get('neighborhood') or ''))
        cluster_text = str(day.get('area_cluster') or '').casefold()
        if (cluster and area and cluster not in area and area not in cluster
                and not re.search(r'\b(?:and|across|throughout)\b|[&,/]', cluster_text)
                and (number, area) not in cluster_noted):
            cluster_noted.add((number, area))
            issues.append(_issue('info', 'candidate_area_outside_day_cluster',
                                 f'Day {number}: source candidate {matched["name"]!r} '
                                 f'is labeled {area!r}, outside the single named '
                                 f'day area {cluster!r}. Confirm the planned route.',
                                 day=number, field='area_cluster'))
    if grounding['unmatched']:
        issues.append(_issue('info', 'places_need_independent_verification',
                             f"{len(grounding['unmatched'])} named places are absent "
                             'from the supplied candidate sample. This is not proof '
                             'that they are fictional.'))
    severities = Counter(item['severity'] for item in issues)
    # Proxy penalty reflects observable quality defects, never source accuracy.
    penalty = sum({'error': 7, 'warning': 4, 'info': 0}[item['severity']]
                  for item in issues)
    return {
        'issues': issues,
        'place_evidence': grounding,
        'route_evidence': route_evidence,
        'issue_counts': dict(severities),
        'score_penalty': min(penalty, 40),
        'review_required': any(item['severity'] in ('error', 'warning') for item in issues),
        'verification_status': 'pending' if cost_count > evidence_count else 'not_assessed',
        'verified_cost_items': evidence_count,
        'cost_items': cost_count,
    }
