"""Provider-grounded travel skills. A retrieved candidate is not a verified claim.

No handler invents operating hours, tickets, reservations or transit schedules.
A missing provider or incomplete source is reported rather than silently guessed.
"""
from __future__ import annotations

import math
import re
from typing import Any

from services.handlers import reply
from services.providers import ProviderUnavailable


def _name(value: Any) -> str:
    return ' '.join(re.findall(r'[^\W_]+', str(value).casefold()))


def _coordinates(args: dict) -> tuple[float, float] | None:
    if 'latitude' not in args or 'longitude' not in args:
        return None
    lat, lon = float(args['latitude']), float(args['longitude'])
    if not math.isfinite(lat) or not math.isfinite(lon) or not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError('Invalid latitude/longitude.')
    return lat, lon


async def restaurant_discovery(ctx, args):
    """Map-backed restaurant *candidates*, without fabricated availability."""
    if ctx.providers is None:
        return reply('A places provider is required for restaurant discovery.', status='unavailable')
    location = str(args.get('city') or args.get('destination') or ctx.fields.get('destination', '')).strip()
    coords = _coordinates(args)
    sources = []
    try:
        if coords is None:
            if not location:
                return reply('Supply a city or coordinates to discover restaurants.', status='partial')
            geocoded = await ctx.providers.request('geocode', place=location)
            sources += geocoded.evidence
            matches = geocoded.data.get('results') or []
            if not matches:
                return reply('No matching city was found by the geocoding provider.', status='partial')
            coords = float(matches[0]['latitude']), float(matches[0]['longitude'])
        result = await ctx.providers.request('places', latitude=coords[0], longitude=coords[1],
                                             kind='restaurant', radius=int(args.get('radius', 4500)))
        sources += result.evidence
    except (ProviderUnavailable, KeyError, ValueError, TypeError) as exc:
        return reply('Restaurant lookup is unavailable; no venues were verified.', status='unavailable',
                     limitations=[str(exc)[:200]])
    venues = []
    for row in (result.data.get('results') or [])[:20]:
        if not isinstance(row, dict) or not isinstance(row.get('name'), str):
            continue
        venues.append({k: row[k] for k in ('name', 'latitude', 'longitude', 'category',
                                           'place_id', 'osm_type', 'osm_id') if k in row})
    return reply('Restaurant candidates found in mapped POI data; hours, menus and availability were not checked.',
                 status='partial' if venues else 'partial',
                 data={'results': venues, 'evidence_level': 'mapped_candidate', 'city': location},
                 sources=sources, limitations=['Map data does not verify menus, prices, opening hours or bookings.'])


async def place_detail_verification(ctx, args):
    """Match a named venue against trusted input candidates, without elevating claims."""
    target = _name(args.get('place') or args.get('name') or '')
    if not target:
        return reply('Specify a named place to check.', status='partial')
    candidates = args.get('candidates') or []
    matched = []
    for row in candidates[:100] if isinstance(candidates, list) else []:
        if isinstance(row, dict) and _name(row.get('name', '')) == target:
            matched.append({k: row[k] for k in ('name', 'latitude', 'longitude', 'category',
                                                 'place_id', 'osm_id', 'osm_type', 'source_url') if k in row})
    if not matched:
        return reply('Place could not be matched to the supplied candidate evidence.', status='partial',
                     data={'place': args.get('place') or args.get('name'), 'match': None,
                           'verification_level': 'unverified'},
                     limitations=['An incomplete candidate set cannot prove a place does not exist.'])
    return reply('An exact name match exists in the supplied candidate data, not a verified venue fact.',
                 status='partial', data={'place': args.get('place') or args.get('name'),
                                         'matches': matched[:5], 'verification_level': 'candidate_match'},
                 limitations=['Name matching does not verify hours, fees, accessibility or location accuracy.'])


async def source_verification(ctx, args):
    """Evaluate evidence-record provenance; never claim a URL verifies a fact."""
    supplied = args.get('sources') or []
    records = []
    if not isinstance(supplied, list):
        return reply('Sources must be a list of structured evidence records.', status='partial')
    for row in supplied[:30]:
        if not isinstance(row, dict):
            continue
        provider = str(row.get('provider') or '').strip()
        url = str(row.get('url') or '')
        records.append({'provider': provider[:100], 'url': url[:500] if url.startswith('https://') else None,
                        'observed_at': str(row.get('observed_at') or '')[:60],
                        'provenance_present': bool(provider and url.startswith('https://'))})
    return reply('Evidence provenance inspected; the factual claim has not been independently confirmed.',
                 status='partial', data={'records': records, 'claim_status': 'verification_pending'},
                 limitations=['Provider metadata and a link do not establish claim truth or source freshness.'])


def _route_candidates(args):
    options = args.get('route_options') or []
    if not isinstance(options, list):
        return []
    valid = []
    for option in options[:12]:
        if not isinstance(option, dict) or not option.get('mode'):
            continue
        row = {k: option[k] for k in ('mode', 'origin', 'destination', 'transfers',
                                      'duration_minutes', 'source', 'source_url') if k in option}
        valid.append(row)
    return valid


async def airport_transfer(ctx, args):
    options = _route_candidates(args)
    origin = str(args.get('origin') or '')[:100]
    destination = str(args.get('destination') or args.get('hotel_area') or '')[:100]
    if not options:
        return reply('Airport ground transport needs sourced route options or a supported transit provider.',
                     status='partial', data={'origin': origin, 'destination': destination,
                                             'route_options': [], 'verification_level': 'unverified'},
                     limitations=['Do not assume a direct airport train serves the hotel district.'])
    # Do not select a route from a list without confirmed endpoints and transfer counts.
    def rank(option):
        dest = _name(option.get('destination', ''))
        exact = bool(dest and dest == _name(destination))
        return (not exact, option.get('transfers', 99) if isinstance(option.get('transfers'), int)
                and not isinstance(option.get('transfers'), bool) else 99)
    ordered = sorted(options, key=rank)
    return reply('Airport transport options ranked by stated destination and transfers; live operation not verified.',
                 status='partial', data={'origin': origin, 'destination': destination,
                                         'route_options': ordered, 'verification_level': 'supplied_options_only'},
                 limitations=['Route descriptions and transit schedules require a current transport source.'])


async def public_transit_router(ctx, args):
    options = _route_candidates(args)
    return reply('Provided transit options organized; no live route or timetable was queried.' if options else
                 'No transit timetable source is configured for this request.', status='partial',
                 data={'route_options': options, 'verification_level': 'supplied_options_only' if options else 'unverified'},
                 limitations=['Confirm endpoints, service dates, transfers and accessibility with the operator.'])


async def travel_cost_estimation(ctx, args):
    """Summarize *supplied* estimates, keeping currency and cost basis separate."""
    items = args.get('items') or []
    if not isinstance(items, list):
        return reply('Provide a list of structured cost items.', status='partial')
    totals: dict[tuple[str, str], float] = {}
    invalid = 0
    for item in items[:100]:
        if not isinstance(item, dict):
            invalid += 1
            continue
        amount, currency, basis = item.get('amount'), str(item.get('currency') or '').upper(), str(item.get('basis') or 'per_person')
        if (isinstance(amount, bool) or not isinstance(amount, (int, float))
                or not math.isfinite(amount) or amount < 0 or not re.fullmatch(r'[A-Z]{3}', currency)
                or basis not in {'per_person', 'group_total'}):
            invalid += 1
            continue
        key = (currency, basis)
        totals[key] = round(totals.get(key, 0) + float(amount), 2)
    return reply('Supplied estimates totaled without currency conversion or price verification.', status='partial',
                 data={'totals': [{'currency': currency, 'basis': basis, 'amount': amount}
                                  for (currency, basis), amount in sorted(totals.items())],
                       'invalid_items': invalid, 'verification_level': 'user_or_model_estimate'},
                 limitations=['These are arithmetic totals, not current supplier quotes.'])
