"""Validated, versioned itineraries for reliable conversational trip editing."""
from __future__ import annotations

from copy import deepcopy
import re
from urllib.parse import urlparse
from typing import Any


class TripValidationError(ValueError):
    """An itinerary update is missing or structurally inconsistent."""


def _clean_text(value: Any, field: str, limit: int = 500) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise TripValidationError(f'{field} must be nonempty text under {limit} characters.')
    return value.strip()



# These fields are optional so previously saved trips remain valid. Limits are
# deliberately small to keep local-model JSON output bounded and auditable.
ACTIVITY_DETAILS = {
    'area': 120,
    'why_this_fits': 260,
    'booking_advice': 260,
    'verification_needed': 260,
    'transport_hint': 180,
    'cost_estimate': 120,
    'place_name': 140,
    'venue_category': 60,
    'cost_currency': 6,
    'evidence_url': 320,
    'evidence_title': 160,
}

ACTIVITY_CATEGORIES = frozenset(('attraction', 'meal', 'transport', 'lodging',
                                'experience', 'rest', 'shopping', 'other'))
MEAL_CATEGORIES = frozenset(('breakfast', 'lunch', 'dinner', 'snack'))
COST_BASIS = frozenset(('per_person', 'per_group', 'per_item', 'unknown'))
EVIDENCE_STATUS = frozenset(('unverified', 'pending', 'verified'))

DAY_DETAILS = {
    'theme': 140,
    'area_cluster': 160,
    'base_city': 140,
    'overnight_base': 180,
    'intercity_transfer': 260,
    'logistics': 360,
    'weather_alternative': 320,
    'budget_note': 260,
}
TRIP_DETAILS = {
    'budget_strategy': 600,
    'arrival_departure_plan': 500,
}


def _optional_text_fields(source: dict, fields: dict[str, int], target: dict) -> None:
    for field, limit in fields.items():
        if field in source and source[field] is not None:
            target[field] = _clean_text(source[field], field, limit)


def _bounded_text_list(source: dict, key: str, max_items: int, limit: int) -> list[str] | None:
    if key not in source:
        return None
    values = source[key]
    if not isinstance(values, list) or len(values) > max_items:
        raise TripValidationError(f'{key} must be a short list.')
    cleaned = [_clean_text(item, key, limit) for item in values]
    # Remove leaked schema tokens, not arbitrary traveler-authored prose.
    placeholder = re.compile(r'(?i)^(?:key_tradeoffs|predeparture_checks|budget_strategy|planning_assumptions)(?:\s+[a-z]{2,25})?$')
    return [item for item in cleaned if not placeholder.fullmatch(item)]


def _day(value: Any, number: int) -> dict:
    if not isinstance(value, dict):
        raise TripValidationError('Each day must be an object.')
    index = value.get('day', value.get('day_number', number))
    if isinstance(index, str):
        match = re.fullmatch(r'(?:day\s*)?(\d{1,2})', index.strip(), flags=re.I)
        index = int(match.group(1)) if match else index
    if isinstance(index, bool) or not isinstance(index, int) or index != number:
        raise TripValidationError('Days must be numbered consecutively from one.')
    title = _clean_text(value.get('title'), 'Day title', 180)
    raw_activities = value.get('activities')
    if not isinstance(raw_activities, list) or not 1 <= len(raw_activities) <= 12:
        raise TripValidationError('Each day needs between 1 and 12 activities.')
    activities = []
    for item in raw_activities:
        if isinstance(item, str):
            activities.append({'description': _clean_text(item, 'Activity')})
        elif isinstance(item, dict):
            description = item.get('description') or item.get('activity') or item.get('name') or item.get('text')
            activity = {'description': _clean_text(description, 'Activity')}
            if isinstance(item.get('time'), str) and item['time'].strip():
                activity['time'] = _clean_text(item['time'], 'Activity time', 70)
            _optional_text_fields(item, ACTIVITY_DETAILS, activity)
            # A qualitative band is not a currency amount. Do not display it
            # twice as both 'cost level' and 'estimated cost'.
            if str(activity.get('cost_estimate') or '').strip().casefold() in {
                    'free', 'low', 'moderate', 'high', 'unknown'}:
                activity.pop('cost_estimate', None)
            for name, allowed in (
                ('activity_type', ACTIVITY_CATEGORIES),
                ('meal_type', MEAL_CATEGORIES),
                ('cost_basis', COST_BASIS),
                ('evidence_status', EVIDENCE_STATUS),
            ):
                if name in item and item[name] is not None:
                    if not isinstance(item[name], str) or item[name] not in allowed:
                        raise TripValidationError(f'Activity {name} is not a supported value.')
                    activity[name] = item[name]
            if item.get('start_time') is not None:
                clock = item['start_time']
                if not isinstance(clock, str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', clock):
                    raise TripValidationError('Activity start_time must use HH:MM.')
                activity['start_time'] = clock
            if activity.get('evidence_url'):
                uri = urlparse(activity['evidence_url'])
                if uri.scheme != 'https' or not uri.hostname or uri.username or uri.password:
                    raise TripValidationError('Activity evidence_url must be an HTTPS link without credentials.')
            # A model-provided URL alone cannot establish a verified price.
            if (activity.get('evidence_status') == 'verified' and
                    not activity.get('evidence_url')):
                raise TripValidationError('Verified evidence needs an HTTPS source URL.')
            # Incoming transfer/buffer belongs to this activity, not the preceding one.
            # A zero-minute transfer is valid when consecutive stops are co-located.
            for field, limit in (('transfer_minutes', 360), ('buffer_minutes', 180)):
                if field in item and item[field] is not None:
                    minutes = item[field]
                    if (isinstance(minutes, bool) or not isinstance(minutes, int)
                            or not 0 <= minutes <= limit):
                        raise TripValidationError(
                            f'Activity {field} must be an integer between 0 and {limit}.')
                    activity[field] = minutes
            duration = item.get('duration_minutes')
            if duration is not None:
                if (isinstance(duration, bool) or not isinstance(duration, int)
                        or not 10 <= duration <= 720):
                    raise TripValidationError('Activity duration must be an integer between 10 and 720 minutes.')
                activity['duration_minutes'] = duration
            cost_level = item.get('cost_level')
            if cost_level is not None:
                if cost_level not in ('free', 'low', 'moderate', 'high', 'unknown'):
                    raise TripValidationError('Activity cost_level must be a supported estimate band.')
                activity['cost_level'] = cost_level
            from .time_contract import activity_start_minute
            activity_start_minute(activity)  # Validate disagreements without rewriting legacy time.
            # Legacy model labels can mix a venue type with a meal slot.
            # Do not let a sightseeing stop masquerade as lodging or a meal.
            if activity.get('activity_type') != 'meal':
                activity.pop('meal_type', None)
            if (activity.get('activity_type') in {'lodging', 'shopping'} and
                    re.match(r'(?i)^(?:visit|explore|tour|see|admire|walk through)\b',
                             activity['description'])):
                activity['activity_type'] = 'attraction'
            activities.append(activity)
        else:
            raise TripValidationError('Activity must be a string or an object.')
    result = {'day': number, 'title': title, 'activities': activities}
    if isinstance(value.get('notes'), str) and value['notes'].strip():
        result['notes'] = _clean_text(value['notes'], 'Day notes', 900)
    _optional_text_fields(value, DAY_DETAILS, result)
    if 'pace' in value and value['pace'] is not None:
        if value['pace'] not in ('light', 'balanced', 'full'):
            raise TripValidationError('Day pace must be light, balanced or full.')
        result['pace'] = value['pace']
    return result



def validate_segment_days(value: Any, *, first: int, last: int) -> list[dict]:
    """Validate a contiguous absolute-numbered batch before merging a longer trip."""
    if (not isinstance(value, dict) or not isinstance(value.get('days'), list)
            or not 1 <= first <= last <= 30):
        raise TripValidationError('A detail segment must contain a valid days list.')
    rows = value['days']
    if len(rows) != last - first + 1:
        raise TripValidationError(f'Detail segment must contain days {first} to {last}.')
    return [_day(row, first + offset) for offset, row in enumerate(rows)]


def validate_itinerary(value: Any, previous: dict | None = None, operation: str = 'create',
                       destination_hint: str | None = None) -> dict:
    """Validate data from an untrusted model and merge constrained revisions.

    A revision must supply either a complete day list or day-specific changes.
    Unchanged days survive a partial change; traveler constraints are merged.
    Unknown price, availability and opening-hours fields are not accepted.
    A destination hint must come from explicit request context, never inference
    from a provider or a prior unrelated trip.
    """
    if not isinstance(value, dict):
        raise TripValidationError('The model must provide a structured itinerary.')
    base = deepcopy(previous) if previous else None
    if operation in {'revise', 'snapshot'} and not base:
        raise TripValidationError('There is no active itinerary to revise.')
    destination = (value.get('destination') or (base or {}).get('destination')
                   or (destination_hint if operation == 'create' else None))
    destination = _clean_text(destination, 'Destination', 180)
    raw_days = value.get('days')
    changes = value.get('changes')
    if operation == 'revise' and raw_days is None and isinstance(changes, list):
        raw_days = deepcopy(base['days'])
        seen = set()
        for entry in changes:
            if not isinstance(entry, dict):
                raise TripValidationError('Day changes must be objects.')
            number = entry.get('day')
            if isinstance(number, bool) or not isinstance(number, int) or number < 1 or number > len(raw_days) or number in seen:
                raise TripValidationError('Invalid or duplicate changed day number.')
            seen.add(number)
            raw_days[number - 1] = entry
        if not changes:
            raise TripValidationError('A revision needs at least one changed day or updated preference.')
    if not isinstance(raw_days, list) or not 1 <= len(raw_days) <= 30:
        raise TripValidationError('An itinerary must contain 1 to 30 days.')
    if base and operation in {'revise', 'snapshot'}:
        if len(raw_days) != len(base['days']):
            raise TripValidationError('A revision cannot silently change the trip duration.')
        if destination.casefold() != base['destination'].casefold():
            raise TripValidationError('A revision cannot silently change the destination.')
    days = [_day(item, index) for index, item in enumerate(raw_days, 1)]
    preferences = ({} if operation == 'snapshot' else
                   dict((base or {}).get('preferences') or {}))
    updates = value.get('preferences') or {}
    if not isinstance(updates, dict) or len(updates) > 25:
        raise TripValidationError('Preferences must be a small object.')
    for key, item in updates.items():
        if not isinstance(key, str) or len(key) > 70 or not isinstance(item, (str, int, float, bool)):
            raise TripValidationError('Preference entries must be short scalar values.')
        if isinstance(item, str) and len(item) > 350:
            raise TripValidationError('Preference values must be short.')
        preferences[key] = item
    summary = value.get('summary') if isinstance(value.get('summary'), str) else None
    title = value.get('title') if isinstance(value.get('title'), str) else None
    result = {
        'destination': destination,
        'title': _clean_text(title or (base or {}).get('title') or f'{destination} itinerary', 'Trip title', 180),
        'days': days,
        'preferences': preferences,
    }
    if summary and summary.strip():
        result['summary'] = _clean_text(summary, 'Trip summary', 1200)
    elif operation == 'revise' and base and base.get('summary'):
        result['summary'] = base['summary']
    for name, limit in TRIP_DETAILS.items():
        if name in value and value[name] is not None:
            result[name] = _clean_text(value[name], name, limit)
        elif operation == 'revise' and base and base.get(name):
            result[name] = base[name]
    for name, count, limit in (
        ('planning_assumptions', 8, 220),
        ('key_tradeoffs', 6, 240),
        ('predeparture_checks', 8, 220),
    ):
        updates = _bounded_text_list(value, name, count, limit)
        if updates is not None:
            result[name] = updates
        elif operation == 'revise' and base and name in base:
            result[name] = deepcopy(base[name])
    return result


def trip_delta(previous: dict | None, current: dict) -> dict:
    if previous is None:
        return {'action': 'created', 'changed_days': [d['day'] for d in current['days']]}
    old = previous['days']
    changed = [row['day'] for row in current['days'] if row != old[row['day'] - 1]]
    prefs = sorted(k for k, v in current.get('preferences', {}).items()
                   if previous.get('preferences', {}).get(k) != v)
    meta = ('summary', 'title', 'budget_strategy', 'arrival_departure_plan',
            'planning_assumptions', 'key_tradeoffs', 'predeparture_checks')
    changed_meta = [name for name in meta if previous.get(name) != current.get(name)]
    return {'action': 'revised', 'changed_days': changed, 'changed_preferences': prefs,
            'changed_trip_fields': changed_meta}


_INTERNAL_REASON = re.compile(
    r'\b(?:aligns? with|matches|fits)\s+(?:the\s+)?[\u2018\u2019\u201c\u201d\'\"]?'
    r'[^.!?]{0,100}\b(?:theme|blueprint|anchor)\b|'
    r'\b(?:primary|core|major|second)\s+anchor\b|'
    r'\b(?:adjusted\s+to\s+\d{1,2}:[0-5]\d|shifted\s+to\s+accommodate)\b', re.I)


def _display_cost_estimate(value: str) -> str:
    # Display source uncertainty only once, irrespective of model formatting.
    text = re.sub(r'(?:\s*\(unverified\))+(?=\s*$)', '', value, flags=re.I).strip()
    return f'{text} (unverified)' if text else 'Unverified'


def render_itinerary(value: dict, *, preface: str = '') -> str:
    """Render only validated travel facts and recorded planning assumptions."""
    lines = []
    if preface.strip():
        lines.append(preface.strip())
    lines.append(f"## {value['title']}")
    if value.get('summary'):
        lines.append(value['summary'])
    for day in value['days']:
        lines.append(f"### Day {day['day']}: {day['title']}")
        if day.get('theme'):
            lines.append(f"Theme: {day['theme']}")
        if day.get('area_cluster'):
            lines.append(f"Area: {day['area_cluster']}")
        if day.get('base_city'):
            lines.append(f"City: {day['base_city']}")
        if day.get('overnight_base'):
            lines.append(f"Overnight base: {day['overnight_base']}")
        if day.get('intercity_transfer'):
            lines.append(f"Intercity transfer: {day['intercity_transfer']}")
        for activity in day['activities']:
            time_label = activity.get('start_time') or activity.get('time')
            time = f"{time_label}: " if time_label else ''
            location = f" ({activity['area']})" if activity.get('area') else ''
            lines.append(f"- {time}{activity['description']}{location}")
            details = []
            for key, title in (
                ('why_this_fits', 'Why'),
                ('transport_hint', 'Transfer'),
                ('booking_advice', 'Booking'),
                ('verification_needed', 'Check'),
            ):
                if not activity.get(key):
                    continue
                if key == 'why_this_fits' and _INTERNAL_REASON.search(activity[key]):
                    # Never silently erase the justification. The source model
                    # should be asked to rewrite it by the planning quality gate;
                    # imported legacy trips still need an honest display fallback.
                    short = re.split(r'[.!?]', activity['description'], maxsplit=1)[0].strip()
                    if len(short) > 115:
                        short = short[:112].rsplit(' ', 1)[0] + '...'
                    details.append(f"Why: Experience: {short}.")
                else:
                    details.append(f"{title}: {activity[key]}")
            if activity.get('duration_minutes'):
                details.append(f"Allow about {activity['duration_minutes']} minutes")
            if activity.get('cost_level') and activity['cost_level'] != 'unknown':
                details.append(f"Cost level: {activity['cost_level']} (indicative)")
            if activity.get('cost_estimate'):
                details.append(f"Estimated cost: {_display_cost_estimate(activity['cost_estimate'])}")
            if details:
                lines.append('  ' + ' | '.join(details))
        for field, title in (
            ('logistics', 'Logistics'),
            ('weather_alternative', 'Alternative'),
            ('budget_note', 'Budget'),
            ('notes', 'Tip'),
        ):
            if day.get(field):
                lines.append(f"{title}: {day[field]}")
    for field, title in (
        ('budget_strategy', 'Budget approach'),
        ('arrival_departure_plan', 'Arrival and departure'),
    ):
        if value.get(field):
            lines.append(f"**{title}:** {value[field]}")
    for field, title in (
        ('planning_assumptions', 'Planning assumptions'),
        ('key_tradeoffs', 'Choices and trade-offs'),
        ('predeparture_checks', 'Check before travel'),
    ):
        if value.get(field):
            lines.append(f"**{title}**")
            lines.extend(f"- {item}" for item in value[field])
    return '\n\n'.join(lines[:2]) + '\n\n' + '\n'.join(lines[2:]) if len(lines) > 2 else '\n\n'.join(lines)
