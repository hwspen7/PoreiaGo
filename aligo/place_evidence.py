"""Structured candidate evidence extracted from explicitly supplied providers.

A name match means only that a supplied source mentions the venue. It does not
verify operating hours, pricing, directions, accessibility, or even the source's
accuracy. A small incomplete candidate set must never serve as a deny-list.
"""
from __future__ import annotations

import json
import re
from typing import Any


def _key(value: Any) -> str:
    return ' '.join(re.findall(r'[^\W_]+', str(value).casefold()))


def _rows(value: Any):
    if isinstance(value, str) and len(value) < 20000:
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return
    if isinstance(value, list):
        for row in value[:35]:
            yield from _rows(row)
    elif isinstance(value, dict):
        yield value
        for key in ('data', 'results', 'items', 'places', 'features'):
            if key in value:
                yield from _rows(value[key])


def extract_place_candidates(evidence: list[dict] | None) -> list[dict[str, str]]:
    """Return small, deduplicated, source-backed place candidates, never facts."""
    candidates: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for row in (evidence or [])[:35]:
        scope = row if isinstance(row, dict) else {}
        for entry in _rows(row):
            name = next((entry[k] for k in ('name', 'place_name', 'venue_name')
                         if isinstance(entry.get(k), str) and entry[k].strip()), None)
            if not name:
                continue
            scope_city = str(entry.get('city') or scope.get('city') or '').strip()
            identifier = (_key(name), _key(scope_city),
                          str(entry.get('place_id') or '').strip())
            if len(identifier[0]) < 4 or identifier in seen:
                continue
            seen.add(identifier)
            candidate = {'name': name.strip()[:160]}
            if scope_city:
                candidate['city'] = scope_city[:100]
            day_scope = entry.get('day', scope.get('day'))
            if isinstance(day_scope, int) and not isinstance(day_scope, bool):
                candidate['candidate_day'] = day_scope
            uri = entry.get('source_url') or entry.get('url')
            if isinstance(uri, str) and uri.startswith('https://'):
                candidate['source_url'] = uri[:500]
            for key in ('area', 'neighborhood', 'place_id', 'address'):
                if isinstance(entry.get(key), str) and entry[key].strip():
                    candidate[key] = entry[key].strip()[:160]
            for key in ('latitude', 'longitude'):
                value = entry.get(key)
                limit = 90 if key == 'latitude' else 180
                if (isinstance(value, (int, float)) and not isinstance(value, bool)
                        and -limit <= value <= limit):
                    candidate[key] = float(value)
            candidates.append(candidate)
            if len(candidates) == 80:
                return candidates
    return candidates


def _scoped_candidate(options: list[dict], day: dict) -> dict | None:
    if not options:
        return None
    city = _key(day.get('base_city') or '')
    if city:
        local = [row for row in options if not row.get('city') or _key(row['city']) == city]
        if not local:
            return None
        options = local
    number = day.get('day')
    return min(options, key=lambda row: (
        0 if row.get('candidate_day') == number else 1,
        0 if city and _key(row.get('city')) == city else 1,
        0 if row.get('place_id') else 1))


def match_place_candidates(itinerary: dict, evidence: list[dict] | None) -> dict:
    """Match explicitly named places against supplied evidence; never infer absence."""
    candidates = extract_place_candidates(evidence)
    if not candidates:
        return {'status': 'no_source_candidates', 'matches': [], 'unmatched': [],
                'candidate_count': 0}
    matches = []
    unmatched = []
    for day in itinerary.get('days', []):
        if not isinstance(day, dict):
            continue
        for index, activity in enumerate(day.get('activities', [])):
            if not isinstance(activity, dict):
                continue
            name = str(activity.get('place_name') or '').strip()
            description = _key(activity.get('description') or '')
            if not name:
                mentioned = [row for row in candidates
                             if len(_key(row['name'])) >= 9 and
                             re.search(r'(?<!\w)' + re.escape(_key(row['name'])) +
                                       r'(?!\w)', description)]
                # Free prose can mention incidental locations. The largest
                # matching named venue is a candidate match, never a fact.
                longest = max((len(_key(row['name'])) for row in mentioned), default=0)
                found = _scoped_candidate([row for row in mentioned
                    if len(_key(row['name'])) == longest], day)
                if found is None:
                    continue
                name = found['name']
            else:
                target = _key(name)
                found = _scoped_candidate([row for row in candidates
                    if _key(row['name']) == target], day)
            record = {'day': day.get('day'), 'activity_index': index, 'name': name}
            if found:
                matches.append({**record, 'candidate': found,
                                'source_kind': 'bounded_skill_candidate',
                                'verified': False})
            else:
                unmatched.append(record)
    return {'status': 'candidate_matching_only', 'matches': matches,
            'unmatched': unmatched, 'candidate_count': len(candidates)}


def analyze_evidence_routes(itinerary: dict, evidence: list[dict] | None) -> dict:
    """Lower-bound walking audit, never a provider routing-time estimate.

    No evidence, no coordinates, or an unspecified transport mode means that
    distance alone cannot establish travel feasibility. Only explicit walking
    with two mapped venues is compared against the free clock interval.
    """
    from math import radians, sin, cos, asin, sqrt, ceil

    matches = match_place_candidates(itinerary, evidence)['matches']
    by_activity = {(row['day'], row['activity_index']): row['candidate']
                   for row in matches}
    warnings = []
    assessed = 0
    for day in itinerary.get('days', []):
        if not isinstance(day, dict):
            continue
        activities = day.get('activities') or []
        for index in range(1, len(activities)):
            before, after = activities[index - 1], activities[index]
            if not isinstance(before, dict) or not isinstance(after, dict):
                continue
            hint = str(after.get('transport_hint') or '').casefold()
            if not re.search(r'\b(?:walk|walking|walkable|on foot)\b', hint):
                continue
            prior_time = str(before.get('start_time') or before.get('time') or '')
            next_time = str(after.get('start_time') or after.get('time') or '')
            if not re.fullmatch(r'[0-2]?\d:[0-5]\d', prior_time) or not re.fullmatch(r'[0-2]?\d:[0-5]\d', next_time):
                continue
            duration = before.get('duration_minutes')
            if isinstance(duration, bool) or not isinstance(duration, int):
                continue
            point_a = by_activity.get((day.get('day'), index - 1))
            point_b = by_activity.get((day.get('day'), index))
            if not point_a or not point_b:
                continue
            if (point_a.get('place_id') and point_b.get('place_id') and
                    point_a['place_id'] == point_b['place_id']):
                continue
            try:
                lat1, lon1 = float(point_a['latitude']), float(point_a['longitude'])
                lat2, lon2 = float(point_b['latitude']), float(point_b['longitude'])
            except (KeyError, TypeError, ValueError):
                continue
            if not (-90 <= lat1 <= 90 and -90 <= lat2 <= 90 and
                    -180 <= lon1 <= 180 and -180 <= lon2 <= 180):
                continue
            def minute(clock: str) -> int:
                h, m = map(int, clock.split(':'))
                return 60 * h + m
            interval = minute(next_time) - minute(prior_time) - duration
            a1, a2 = radians(lat1), radians(lat2)
            dlat, dlon = a2-a1, radians(lon2-lon1)
            arc = 2 * asin(min(1.0, sqrt(sin(dlat/2)**2 +
                                       cos(a1)*cos(a2)*sin(dlon/2)**2)))
            distance = 6371.0088 * arc
            assessed += 1
            # Straight-line distance is a strict lower bound on any walking
            # route. Six km/h is deliberately generous for screening only.
            minimum_minutes = ceil(distance / 6.0 * 60)
            required = minimum_minutes + max(0, after.get('buffer_minutes') or 0)
            if distance > .12 and interval < required:
                warnings.append({
                    'day': day.get('day'), 'activity_index': index,
                    'distance_km_straight_line': round(distance, 2),
                    'available_minutes': interval,
                    'minimum_minutes_at_6kmh': minimum_minutes,
                    'message': (f"Day {day.get('day')} activities {index} and {index + 1}: "
                                f"walking requires at least {minimum_minutes} minutes "
                                f"based on mapped straight-line distance, but only "
                                f"{interval} minutes are available. Verify the route "
                                "and adjust the schedule.")})
    return {'assessed_walking_legs': assessed, 'warnings': warnings,
            'method': 'straight_line_lower_bound_6kmh_not_real_routing'}
