"""Bounded, source-backed transport and supplier evidence for daily planning.

This module never treats model estimates, map discovery, test offers, or
budget allocations as live supplier quotes. Online calls are optional and
fail closed to the existing offline planning path.
"""
from __future__ import annotations

import asyncio
import math
import re
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from .providers import ProviderUnavailable


_REQUEST_ERRORS = (ProviderUnavailable, asyncio.TimeoutError, ValueError, TypeError,
                   KeyError, RuntimeError, LookupError, OSError)


def _available(providers: Any, capability: str) -> bool:
    method = getattr(providers, 'available', None)
    if not callable(method):
        return False
    try:
        return bool(method(capability))
    except (ValueError, KeyError, TypeError):
        return False


def _coordinate(item: dict) -> dict | None:
    try:
        latitude, longitude = float(item['latitude']), float(item['longitude'])
    except (TypeError, ValueError, KeyError, OverflowError):
        return None
    if (not math.isfinite(latitude) or not math.isfinite(longitude)
            or not -90 <= latitude <= 90 or not -180 <= longitude <= 180):
        return None
    return {'latitude': latitude, 'longitude': longitude}


def _mode(fields: dict) -> str | None:
    requested = str(fields.get('transport_mode') or fields.get('travel_mode')
                    or fields.get('preferred_transport') or 'walk').casefold().strip()
    return {'walking': 'walk', 'walk': 'walk', 'on foot': 'walk',
            'driving': 'drive', 'car': 'drive', 'taxi': 'drive', 'drive': 'drive',
            'bike': 'bicycle', 'cycling': 'bicycle', 'bicycle': 'bicycle'}.get(requested)


def _cell(matrix: list, i: int, j: int) -> dict | None:
    try:
        row = matrix[i]
        entry = row[j] if isinstance(row, list) else row['targets'][j]
        if not isinstance(entry, dict):
            return None
        distance, duration = float(entry['distance']), float(entry['time'])
        if not (math.isfinite(distance) and math.isfinite(duration)
                and 0 <= distance <= 200_000 and 0 <= duration <= 100_000):
            return None
        return {'distance_m': round(distance), 'duration_minutes': math.ceil(duration / 60)}
    except (IndexError, KeyError, TypeError, ValueError, OverflowError):
        return None


async def route_evidence(providers: Any, candidates: list[dict], fields: dict,
                         *, timeout: float = 8) -> dict:
    """Check a small, coordinate-backed venue set via Geoapify matrix.

    The matrix is not evidence of scheduled transit, real-time traffic, or a
    verified visit. Partial/invalid responses do not become guessed durations.
    """
    mode = _mode(fields)
    if not mode or not _available(providers, 'route_matrix'):
        return {}
    places = []
    names = set()
    for item in candidates:
        if not isinstance(item, dict):
            continue
        name = str(item.get('name') or '').strip()
        coords = _coordinate(item)
        if not name or not coords or name.casefold() in names:
            continue
        names.add(name.casefold())
        places.append({'name': name[:130], **coords})
        if len(places) == 4:
            break
    if len(places) < 2:
        return {}
    try:
        result = await asyncio.wait_for(providers.request(
            'route_matrix', sources=places, targets=places, mode=mode), timeout=timeout)
        matrix = result.data.get('sources_to_targets')
        if not isinstance(matrix, list) or len(matrix) != len(places):
            return {}
        legs = []
        for i, origin in enumerate(places):
            for j, destination in enumerate(places):
                if i == j:
                    continue
                cell = _cell(matrix, i, j)
                if cell is not None:
                    legs.append({'from': origin['name'], 'to': destination['name'], **cell})
        if not legs:
            return {}
        sources = [{'provider': str(getattr(e, 'provider', ''))[:60],
                    'url': str(getattr(e, 'url', ''))[:200]}
                   for e in (getattr(result, 'evidence', None) or [])[:2]]
        return {'status': 'provider_route', 'mode': mode, 'units': 'minutes_and_meters',
                'legs': legs, 'sources': sources, 'checked_places': len(places),
                'limitations': ('Calculated road-network durations for candidate venues only. '
                                'Not live traffic, service schedules, or proof that a venue is open.')}
    except _REQUEST_ERRORS:
        return {}


def _clock(value: Any) -> int | None:
    text = str(value or '')
    if not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', text):
        return None
    hours, minutes = map(int, text.split(':'))
    return hours * 60 + minutes


def verified_day_route_issues(day: dict, evidence: dict) -> list[str]:
    """Check only consecutive, name-matched visits when walking is explicit.

    Never reject an unknown transfer type based on a walking-only matrix.
    """
    if evidence.get('status') != 'provider_route' or evidence.get('mode') != 'walk':
        return []
    travel = {(leg['from'].casefold(), leg['to'].casefold()): leg
              for leg in evidence.get('legs', []) if isinstance(leg, dict)}
    problems = []
    activities = day.get('activities') or []
    for left, right in zip(activities, activities[1:]):
        name_a = str(left.get('place_name') or '').strip().casefold()
        name_b = str(right.get('place_name') or '').strip().casefold()
        leg = travel.get((name_a, name_b))
        if not leg:
            continue
        # A mode explicitly described as taxi/metro/etc. cannot be checked
        # against a walking-only matrix.
        transport = str(right.get('transport_hint') or '').casefold().strip()
        if not re.search(r'\b(walk|walking|on foot)\b', transport):
            continue
        start_a, start_b = _clock(left.get('start_time')), _clock(right.get('start_time'))
        duration = left.get('duration_minutes')
        if (start_a is None or start_b is None or not isinstance(duration, int)
                or isinstance(duration, bool) or duration <= 0):
            continue
        free = start_b - (start_a + duration)
        required = leg['duration_minutes']
        if free < required:
            problems.append(f'Verified walking transfer from {left.get("place_name")} to '
                            f'{right.get("place_name")} requires about {required} minutes; '
                            f'the proposed schedule allows only {max(0, free)} minutes. '
                            'Adjust the next start time or select a different transport mode '
                            'and verify its route.')
    return problems[:4]


def _date(value: Any) -> str | None:
    try:
        if not isinstance(value, str):
            return None
        return date.fromisoformat(value).isoformat() if re.fullmatch(r'\d{4}-\d{2}-\d{2}', value) else None
    except ValueError:
        return None


def _count(value: Any, low: int, high: int) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if low <= number <= high else None


def _amount(value: Any) -> str | None:
    if not isinstance(value, (int, float, str)) or isinstance(value, bool):
        return None
    try:
        amount = Decimal(str(value))
        return str(amount) if amount.is_finite() and amount >= 0 else None
    except InvalidOperation:
        return None


def _compact_price(value: Any, depth: int = 0) -> dict:
    """Preserve bounded structured price components without inventing a total."""
    if not isinstance(value, dict) or depth > 2:
        return {}
    result = {}
    for key, component in list(value.items())[:12]:
        name = str(key)[:45]
        if not name:
            continue
        if isinstance(component, dict) and depth < 2:
            nested = _compact_price(component, depth + 1)
            if nested:
                result[name] = nested
        elif isinstance(component, (int, float, str)) and not isinstance(component, bool):
            if len(str(component)) <= 90:
                result[name] = component
    return result


def _currency(value: Any) -> str | None:
    text = str(value or '').upper()
    return text if re.fullmatch(r'[A-Z]{3}', text) else None


async def supplier_price_evidence(providers: Any, fields: dict,
                                  *, timeout: float = 12) -> dict:
    """Request bounded offers only with explicit search inputs.

    No city-to-airport guessing, no generic country-wide hotel quote,
    and no sandbox result presented as a live market price.
    """
    result: dict[str, dict] = {}
    origin = str(fields.get('origin_airport') or fields.get('flight_origin') or '').upper()
    destination = str(fields.get('destination_airport') or fields.get('flight_destination') or '').upper()
    departure = _date(fields.get('departure_date') or fields.get('flight_date'))
    passengers = _count(fields.get('travelers', 1), 1, 8)
    if (_available(providers, 'flights') and departure and passengers and origin != destination
            and re.fullmatch(r'[A-Z]{3}', origin) and re.fullmatch(r'[A-Z]{3}', destination)):
        try:
            response = await asyncio.wait_for(providers.request(
                'flights', origin=origin, destination=destination,
                departure_date=departure, passengers=passengers), timeout=timeout)
            data = response.data
            mode = str(data.get('mode') or 'unknown').casefold()
            offers = []
            for row in (data.get('offers') or [])[:3]:
                if not isinstance(row, dict):
                    continue
                amount, currency = _amount(row.get('amount')), _currency(row.get('currency'))
                if amount and currency:
                    offers.append({'amount': amount, 'currency': currency,
                                   'expires_at': str(row.get('expires_at') or '')[:35]})
            if offers:
                result['flights'] = {'origin': origin, 'destination': destination,
                                     'departure_date': departure, 'travelers': passengers,
                                     'offers': offers,
                                     'verification_level': ('live_supplier_quote' if mode == 'live'
                                                            else 'test_offer_not_bookable'),
                                     'source': 'duffel', 'environment': mode}
        except _REQUEST_ERRORS:
            pass
    checkin = _date(fields.get('checkin') or fields.get('hotel_checkin'))
    checkout = _date(fields.get('checkout') or fields.get('hotel_checkout'))
    city_id = _count(fields.get('booking_city_id'), 1, 10**10)
    adults = _count(fields.get('adults', fields.get('travelers', 2)), 1, 16)
    rooms = _count(fields.get('rooms', 1), 1, 8)
    country = str(fields.get('booker_country') or 'us')
    if (_available(providers, 'accommodation_offers') and checkin and checkout and checkin < checkout
            and city_id and adults and rooms and re.fullmatch(r'[A-Za-z]{2}', country)):
        try:
            response = await asyncio.wait_for(providers.request(
                'accommodation_offers', checkin=checkin, checkout=checkout,
                adults=adults, rooms=rooms, booker_country=country, city_id=city_id),
                timeout=timeout)
            data = response.data
            mode = str(data.get('inventory_mode') or 'unknown').casefold()
            offers = []
            for row in (data.get('results') or [])[:3]:
                if not isinstance(row, dict):
                    continue
                price = row.get('price')
                # Booking Demand API price structures vary by inventory version.
                # Preserve only a bounded, structured quote; do not manufacture a total.
                if isinstance(price, dict) and price:
                    offers.append({'property_id': str(row.get('id') or '')[:80],
                                   'name': str(row.get('name') or '')[:100],
                                   'price': _compact_price(price)})
            offers = [row for row in offers if row['price']]
            if offers:
                result['accommodation'] = {'checkin': checkin, 'checkout': checkout,
                                           'adults': adults, 'rooms': rooms, 'offers': offers,
                                           'verification_level': ('live_supplier_quote' if mode == 'live'
                                                                  else 'sandbox_inventory_not_bookable'),
                                           'source': 'booking-demand', 'environment': mode}
        except _REQUEST_ERRORS:
            pass
    return result
