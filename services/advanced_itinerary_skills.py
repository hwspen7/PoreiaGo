"""Evidence-conscious specialist travel skills for itinerary-first planning.

Each function operates on bounded supplied records. Missing factual inputs remain
unknown; none of these functions invents providers, schedules, prices or routes.
"""
from __future__ import annotations

from datetime import date
import math
import re
from typing import Any

from services.skill_support import reply, slug, place_record
from services.providers import ProviderUnavailable


def _rows(args: dict, key: str = 'candidates', maximum: int = 100) -> list[dict] | None:
    source = args.get(key, [])
    if not isinstance(source, list):
        return None
    return [r for r in source[:maximum] if isinstance(r, dict)]


def _name(row: dict) -> str:
    return str(row.get('name') or row.get('title') or '').strip()[:150]


def _identity(row: dict) -> str:
    return str(row.get('place_id') or row.get('event_id') or row.get('osm_id') or
               f"{slug(_name(row))}|{slug(row.get('address'))}")


def _record(row: dict) -> dict:
    rec = place_record(row)
    rec['name'] = _name(row)
    for key in ('event_id', 'start_date', 'end_date', 'category', 'source_url', 'venue',
                'distance_km', 'travel_minutes', 'accessibility', 'season_months'):
        if key in row and key not in rec:
            value = row[key]
            rec[key] = value[:250] if isinstance(value, str) else value
    return rec


def _score_terms(row: dict, terms: list[str]) -> int:
    content = slug(' '.join(str(row.get(k) or '') for k in
                            ('name', 'description', 'category', 'cuisine', 'tags', 'neighborhood', 'area')))
    return sum(1 for word in terms if word and word in content)


def _terms(value: Any) -> list[str]:
    values = value if isinstance(value, list) else [value]
    return [slug(s) for s in values[:15] if isinstance(s, str) and slug(s)]


def _limit(args: dict, default: int = 6) -> int:
    try:
        n = int(args.get('limit', default))
    except (TypeError, ValueError, OverflowError):
        raise ValueError('Invalid result limit.') from None
    if n < 1 or n > 30:
        raise ValueError('Result limit must be between 1 and 30.')
    return n


def _iso(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value[:10]) if re.fullmatch(r'\d{4}-\d{2}-\d{2}', value[:10]) else None
    except ValueError:
        return None


def _minutes(value: Any) -> int | None:
    if not isinstance(value, str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', value):
        return None
    return int(value[:2])*60 + int(value[3:])


def _safe_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


async def neighborhood_intelligence(ctx, args):
    source = _rows(args)
    if source is None:
        return reply('Neighborhood candidates must be a list.', status='partial')
    desired = _terms(args.get('interests'))
    groups: dict[str, dict] = {}
    for row in source:
        region = str(row.get('neighborhood') or row.get('area') or '').strip()
        if not region or not _name(row):
            continue
        key = slug(region)
        if key not in groups:
            groups[key] = {'name': region[:100], 'venues': [], 'matched_interests': 0}
        groups[key]['venues'].append(_record(row))
        groups[key]['matched_interests'] += _score_terms(row, desired)
    ranked = sorted(groups.values(), key=lambda item: (-item['matched_interests'], -len(item['venues']), item['name']))
    return reply('Neighborhood clusters derived from venue locality fields; atmosphere and safety are not verified.',
                 status='partial', data={'results': [{**g, 'venue_count': len(g['venues']),
                           'venues': g['venues'][:4]} for g in ranked[:_limit(args)]],
                            'unassigned_venues': sum(bool(_name(r)) and not (r.get('area') or r.get('neighborhood')) for r in source),
                            'verification_level': 'candidate_metadata'},
                 limitations=['Venue density is sample-based, not a complete neighborhood survey.',
                              'No claim is made about local safety or neighborhood atmosphere.'])


async def opening_hours_intelligence(ctx, args):
    source = _rows(args)
    if source is None:
        return reply('Venues must be a list.', status='partial')
    visit = _iso(args.get('visit_date'))
    weekday = visit.strftime('%a').lower() if visit else None
    results = []
    for row in source:
        if not _name(row):
            continue
        schedule = row.get('weekly_hours')
        schedule = schedule if isinstance(schedule, dict) else {}
        windows = schedule.get(weekday, []) if weekday else []
        if isinstance(windows, dict):
            windows = [windows]
        windows = windows if isinstance(windows, list) else []
        parsed = []
        for w in windows[:8]:
            if isinstance(w, dict) and _minutes(w.get('open')) is not None and _minutes(w.get('close')) is not None:
                if _minutes(w['close']) > _minutes(w['open']):
                    parsed.append({'open': w['open'], 'close': w['close']})
        closed = weekday is not None and weekday in schedule and not windows
        status = ('known_closed_in_supplied_schedule' if closed else
                  'scheduled_hours_supplied' if parsed else 'unknown')
        results.append({'name': _name(row), 'place_id': row.get('place_id'), 'visit_date': str(visit) if visit else None,
                        'hours_status': status, 'windows': parsed,
                        'raw_hours': str(row.get('opening_hours') or '')[:180] or None,
                        'verification_level': 'supplied_schedule_unverified'})
    return reply('Supplied opening-hour metadata organized; no live venue schedule was checked.',
                 status='partial', data={'results': results[:_limit(args, 12)],
                                         'timezone_status': 'not_verified'},
                 limitations=['Holidays, seasonal closures and source freshness require direct confirmation.',
                              'A free-text opening_hours tag is not interpreted as a confirmed schedule.'])


async def ticket_entry_planning(ctx, args):
    source = _rows(args)
    if source is None:
        return reply('Ticket candidates must be a list.', status='partial')
    results = []
    for row in source:
        if not _name(row):
            continue
        required = row.get('reservation_required')
        required = required if isinstance(required, bool) else None
        price = _safe_number(row.get('ticket_price'))
        currency = str(row.get('currency') or '').upper()
        if currency and not re.fullmatch(r'[A-Z]{3}', currency):
            price = None
        result = {'name': _name(row), 'entry_status': 'not_checked',
                  'reservation_required_claim': required, 'booking_status': 'not_checked',
                  'supplier_reference': str(row.get('booking_url') or '')[:250] or None,
                  'listed_price': price if price is not None and price >= 0 and currency else None,
                  'currency': currency if price is not None and currency else None,
                  'listed_entry_windows': (row.get('entry_windows') or [])[:8]
                        if isinstance(row.get('entry_windows'), list) else [],
                  'verification_level': 'supplied_metadata_unverified'}
        results.append(result)
    return reply('Entry requirements and supplied booking references assembled; no reservations or prices verified.',
                 status='partial', data={'results': results[:_limit(args, 12)]},
                 limitations=['Check live availability, official ticket conditions and date-specific prices.',
                              'A booking URL is not a confirmed admission or reservation.'])


async def local_events_discovery(ctx, args):
    source = _rows(args, 'events')
    if source is None:
        return reply('Event candidates must be a list.', status='partial')
    sources = []
    gateway = getattr(ctx, 'providers', None)
    enabled = getattr(gateway, 'available', None)
    can_discover = False
    if callable(enabled):
        try:
            can_discover = bool(enabled('events'))
        except (ValueError, KeyError, TypeError):
            pass
    if not source and can_discover and args.get('city'):
        try:
            provider_result = await gateway.request('events',
                city=str(args['city'])[:100],
                start_date=str(args.get('start_date') or '')[:10],
                end_date=str(args.get('end_date') or '')[:10])
            provider_rows = provider_result.data.get('results') or []
            if isinstance(provider_rows, list):
                source = [item for item in provider_rows[:100] if isinstance(item, dict)]
            sources = provider_result.evidence
        except (ProviderUnavailable, ValueError, TypeError, KeyError):
            pass
    first, last = _iso(args.get('start_date')), _iso(args.get('end_date'))
    if first and last and first > last:
        return reply('Travel date range is reversed.', status='partial')
    interests = _terms(args.get('interests'))
    ranked, seen, undated = [], set(), 0
    for row in source:
        if not _name(row):
            continue
        begin, end = _iso(row.get('start_date')), _iso(row.get('end_date'))
        if not begin:
            undated += 1
            continue
        end = end or begin
        if end < begin or (first and end < first) or (last and begin > last):
            continue
        identity = _identity(row)
        if identity in seen:
            continue
        seen.add(identity)
        ranked.append((_score_terms(row, interests), begin, _record(row)))
    ranked.sort(key=lambda r: (-r[0], r[1], r[2]['name']))
    return reply('Dated event candidates filtered to the supplied trip window; dates are not live-verified.',
                 status='partial', data={'results': [r[2] for r in ranked[:_limit(args)]],
                                         'undated_omitted': undated, 'event_status': 'candidate_only'},
                 sources=sources, limitations=['Event changes, cancellations, tickets and official schedules require confirmation.',
                              'Events not backed by fresh provider evidence still require independent date confirmation.'])


async def culinary_specialty_discovery(ctx, args):
    source = _rows(args)
    if source is None:
        return reply('Culinary candidates must be a list.', status='partial')
    preferences = set(_terms(args.get('interests'))) | set(_terms(args.get('cuisines')))
    avoid = set(_terms(args.get('avoid_ingredients'))) | set(_terms(args.get('avoid_names')))
    ranked, seen = [], set()
    for row in source:
        if not _name(row):
            continue
        identity = _identity(row)
        if identity in seen:
            continue
        seen.add(identity)
        content = slug(' '.join(str(row.get(k) or '') for k in ('name', 'cuisine', 'description', 'ingredients', 'tags')))
        if any(word in content for word in avoid):
            continue
        score = _score_terms(row, list(preferences))
        record = _record(row)
        record['cuisine'] = str(row.get('cuisine') or '')[:100]
        record['specialties'] = [str(x)[:100] for x in (row.get('specialties') or [])[:6]] if isinstance(row.get('specialties'), list) else []
        record['dietary_status'] = 'not_verified'
        ranked.append((score, record))
    ranked.sort(key=lambda x: (-x[0], x[1]['name']))
    return reply('Food and dining candidates ranked from supplied metadata; regional specialties are not inferred.',
                 status='partial', data={'results': [r for _, r in ranked[:_limit(args)]],
                                         'verification_level': 'candidate_metadata'},
                 limitations=['Cuisine tags do not prove authenticity, ingredients, allergy safety or menu availability.'])


async def seasonal_experience_intelligence(ctx, args):
    source = _rows(args)
    if source is None:
        return reply('Seasonal candidates must be a list.', status='partial')
    try:
        month = int(args.get('month'))
    except (TypeError, ValueError):
        return reply('Specify the travel month from 1 to 12.', status='partial')
    if month not in range(1, 13):
        return reply('Travel month must be between 1 and 12.', status='partial')
    results, unknown = [], 0
    for row in source:
        if not _name(row):
            continue
        months = row.get('season_months')
        if not isinstance(months, list) or any(type(m) is not int or not 1 <= m <= 12 for m in months):
            unknown += 1
            continue
        if month in months:
            result = _record(row)
            result['seasonal_match'] = 'listed_for_month'
            result['verification_level'] = 'supplied_seasonality_unverified'
            results.append(result)
    return reply('Seasonal experiences matched to supplied month tags without forecasting conditions.',
                 status='partial', data={'results': results[:_limit(args)], 'month': month,
                                         'missing_season_metadata': unknown},
                 limitations=['Climate, bloom dates, wildlife and seasonal operating schedules vary by year.',
                              'Month tags are supplied claims, not confirmed conditions.'])


async def accessibility_suitability(ctx, args):
    source = _rows(args)
    if source is None:
        return reply('Accessibility candidates must be a list.', status='partial')
    required = [str(x)[:60] for x in args.get('requirements', [])[:12]] if isinstance(args.get('requirements'), list) else []
    results = []
    for row in source:
        if not _name(row):
            continue
        attrs = row.get('accessibility')
        attrs = attrs if isinstance(attrs, dict) else {}
        checks = {k: attrs.get(k) if isinstance(attrs.get(k), bool) else None for k in required}
        results.append({'name': _name(row), 'requirements': checks,
                        'clearly_marked_unavailable': [k for k, v in checks.items() if v is False],
                        'unknown_requirements': [k for k, v in checks.items() if v is None],
                        'verification_level': 'supplied_accessibility_unverified'})
    return reply('Accessibility attributes screened against stated needs; absence of data is unknown, not accessible.',
                 status='partial', data={'results': results[:_limit(args, 12)]},
                 limitations=['Confirm ramps, gradients, lifts, restrooms and temporary barriers with venues.',
                              'Do not treat an unknown attribute as a guarantee of accessibility.'])


async def day_trip_discovery(ctx, args):
    source = _rows(args)
    if source is None:
        return reply('Day-trip candidates must be a list.', status='partial')
    max_distance = _safe_number(args.get('max_distance_km', 140))
    if max_distance is None or not 1 <= max_distance <= 1000:
        return reply('Invalid maximum day-trip distance.', status='partial')
    ranked = []
    for row in source:
        if not _name(row):
            continue
        distance = _safe_number(row.get('distance_km'))
        if distance is None or distance < 0 or distance > max_distance:
            continue
        rec = _record(row)
        rec['distance_km'] = round(distance, 1)
        rec['route_time_status'] = 'not_verified'
        rec['return_journey_status'] = 'not_verified'
        ranked.append(rec)
    ranked.sort(key=lambda r: (r['distance_km'], r['name']))
    return reply('Nearby day-trip candidates filtered by supplied distances; return travel is unverified.',
                 status='partial', data={'results': ranked[:_limit(args)], 'origin': str(args.get('origin') or '')[:100]},
                 limitations=['Supplied distance is not a live road or transit estimate.',
                              'Verify return transport, operating hours and trip duration independently.'])


async def itinerary_feasibility_audit(ctx, args):
    activities = _rows(args, 'activities', maximum=60)
    if activities is None:
        return reply('Activities must be a list.', status='partial')
    risks = []
    last_end: int | None = None
    visited: set[str] = set()
    for index, item in enumerate(activities):
        place = slug(item.get('place_name') or item.get('place') or '')
        if place and place in visited:
            risks.append({'type': 'repeated_place', 'activity_index': index, 'place': place})
        if place:
            visited.add(place)
        start = _minutes(item.get('start_time') or item.get('time'))
        duration = item.get('duration_minutes')
        duration = duration if type(duration) is int and 0 < duration <= 1440 else None
        if start is not None and last_end is not None and start < last_end:
            risks.append({'type': 'time_overlap', 'activity_index': index})
        if start is not None and duration is not None:
            last_end = start + duration
        else:
            last_end = None
        if item.get('verification_level') in {'unknown', 'unverified'}:
            risks.append({'type': 'unverified_place', 'activity_index': index})
    return reply('Non-destructive feasibility audit completed; flagged issues require model or traveler review.',
                 status='partial', data={'risks': risks[:30], 'checked_activities': len(activities),
                                         'auto_modified': False},
                 limitations=['No route, live schedule, venue admission or activity descriptions were independently verified.',
                              'A risk flag is not proof a visit is impossible.'])


IMPLEMENTATIONS = {
    'neighborhood-intelligence': neighborhood_intelligence,
    'opening-hours-intelligence': opening_hours_intelligence,
    'ticket-entry-planning': ticket_entry_planning,
    'local-events-discovery': local_events_discovery,
    'culinary-specialty-discovery': culinary_specialty_discovery,
    'seasonal-experience-intelligence': seasonal_experience_intelligence,
    'accessibility-suitability': accessibility_suitability,
    'day-trip-discovery': day_trip_discovery,
    'itinerary-feasibility-audit': itinerary_feasibility_audit,
}
