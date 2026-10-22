"""Bounded, non-authoritative provider review of edited itinerary places.

A map name match establishes only that a candidate appears in the provider
response. It never verifies tickets, prices, hours, reservations or travel time.
This service never changes the validated itinerary or claims to certify places.
"""
from __future__ import annotations

import asyncio
import os
import re
from datetime import datetime, timezone
from typing import Any

from .providers import ProviderUnavailable


def _name(value: Any) -> str:
    return ' '.join(re.findall(r'[^\W_]+', str(value or '').casefold()))


def _bound(name: str, default: int, limit: int) -> int:
    try:
        return max(1, min(limit, int(os.getenv(name, str(default)))))
    except (TypeError, ValueError):
        return default


def changed_place_visits(previous: dict, proposed: dict) -> list[dict]:
    """Select new, renamed or rescheduled explicitly named places only."""
    original_days = {day['day']: day for day in (previous or {}).get('days', [])}
    changes = []
    for day in proposed.get('days', []):
        original = original_days.get(day['day'], {})
        if day == original:
            continue
        city = str(day.get('base_city') or '').strip()
        # A one-city destination is usable as an explicit fallback only for
        # single-day plans. Never guess a city from a multi-region title.
        if not city and len(proposed.get('days', [])) == 1:
            city = str(proposed.get('destination') or '').strip()
        before = {
            (_name(a.get('place_name')), a.get('start_time'), a.get('duration_minutes'))
            for a in original.get('activities', []) if a.get('place_name')
        }
        for index, activity in enumerate(day.get('activities', []), 1):
            if activity.get('activity_type') in {'transport', 'rest', 'lodging'}:
                continue
            place = str(activity.get('place_name') or '').strip()
            identity = (_name(place), activity.get('start_time'), activity.get('duration_minutes'))
            if not place or identity in before:
                continue
            kind = 'restaurant' if activity.get('activity_type') == 'meal' else 'tourism'
            changes.append({'day': day['day'], 'activity_index': index,
                            'name': place[:140], 'city': city[:100], 'kind': kind})
    # De-duplicate repeated place/time identities without suppressing distinct days.
    return changes


def _source_metadata(evidence: list, *, freshness_seconds: int) -> dict:
    """Classify a provider observation, not its individual factual claims."""
    if not evidence:
        return {'freshness': 'unknown'}
    source = evidence[0]
    observed = getattr(source, 'observed_at', None)
    provider = getattr(source, 'provider', None)
    report = {'freshness': 'unknown'}
    if isinstance(provider, str) and provider.strip():
        report['provider'] = provider[:80]
    if not isinstance(observed, str):
        return report
    try:
        stamp = datetime.fromisoformat(observed.replace('Z', '+00:00'))
        if stamp.tzinfo is None:
            return report
        elapsed = (datetime.now(timezone.utc) - stamp.astimezone(timezone.utc)).total_seconds()
        if elapsed < -300:
            return report
        report['observed_at'] = observed[:60]
        report['freshness'] = ('recent_observation' if elapsed <= freshness_seconds
                               else 'stale_observation')
    except (ValueError, OverflowError):
        pass
    return report


async def audit_revision_place_evidence(previous: dict, proposed: dict, providers,
                                        *, max_stops: int | None = None) -> dict:
    """Re-query bounded changed venues when a real places provider is enabled.

    Nonmatches never imply that a place does not exist. A successful map result
    is still a candidate and MUST NOT elevate evidence_status to verified.
    """
    visits = changed_place_visits(previous, proposed)
    budget = max_stops if max_stops is not None else _bound('ALIGO_REVISION_EVIDENCE_STOPS', 6, 12)
    budget = max(1, min(int(budget), 12))
    result = {'status': 'no_named_place_changes', 'changed_places': len(visits),
              'reviewed_places': 0, 'unreviewed_places': max(0, len(visits) - budget),
              'places': [], 'limitations': [
                  'Map candidates do not verify opening hours, prices, tickets, bookings or transit.']}
    if not visits:
        return result
    work = visits[:budget]
    result['unreviewed_places'] += sum(not row['city'] for row in work)
    eligible = bool(providers and callable(getattr(providers, 'available', None)) and
                    providers.available('geocode') and providers.available('places'))
    # Always return bounded per-stop statuses, even in offline mode.
    if not eligible:
        result['status'] = 'provider_unavailable'
        result['places'] = [{**item, 'status': 'unverified_offline'} for item in work]
        return result
    max_cities = _bound('ALIGO_REVISION_EVIDENCE_CITIES', 2, 4)
    freshness = _bound('ALIGO_REVISION_EVIDENCE_FRESHNESS_SECONDS', 86400, 604800)
    unique = list(dict.fromkeys((v['city'], v['kind']) for v in work if v['city']))
    cache: dict[tuple[str, str], tuple[list, list, str]] = {}
    for city, kind in unique[:max_cities]:
        try:
            geocoded = await providers.request('geocode', place=city)
            points = geocoded.data.get('results') or []
            if not points:
                cache[(city, kind)] = ([], [], 'city_unresolved')
                continue
            point = points[0]
            latitude, longitude = float(point['latitude']), float(point['longitude'])
            places = await providers.request('places', latitude=latitude, longitude=longitude,
                                             kind=kind, radius=6500)
            rows = [row for row in (places.data.get('results') or [])[:100]
                    if isinstance(row, dict) and isinstance(row.get('name'), str)]
            cache[(city, kind)] = (rows, list(places.evidence), 'ok')
        except (ProviderUnavailable, KeyError, TypeError, ValueError, asyncio.TimeoutError):
            cache[(city, kind)] = ([], [], 'provider_error')
    for item in work:
        if not item['city']:
            row = {**item, 'status': 'city_unspecified'}
        elif (item['city'], item['kind']) not in cache:
            row = {**item, 'status': 'review_budget_exceeded'}
        else:
            candidates, sources, state = cache[(item['city'], item['kind'])]
            matches = [candidate for candidate in candidates
                       if _name(candidate.get('name')) == _name(item['name'])]
            if state != 'ok':
                row = {**item, 'status': state}
            elif len(matches) == 1:
                row = {**item, 'status': 'mapped_candidate',
                       'identity_level': 'exact_normalized_name',
                       **_source_metadata(sources, freshness_seconds=freshness)}
                matched = matches[0]
                if isinstance(matched.get('place_id'), (str, int)):
                    row['place_id'] = str(matched['place_id'])[:100]
            elif len(matches) > 1:
                row = {**item, 'status': 'ambiguous_name_match'}
            else:
                row = {**item, 'status': 'no_candidate_match'}
        result['places'].append(row)
    result['reviewed_places'] = sum(row['status'] in {'mapped_candidate',
                                 'ambiguous_name_match', 'no_candidate_match'}
                                 for row in result['places'])
    result['status'] = 'review_complete' if result['reviewed_places'] == len(work) else 'partial'
    return result


def cross_day_transfer_uncertainties(document: dict) -> list[dict]:
    """Flag absent evidence for explicit intercity transitions; never guess ETAs."""
    concerns = []
    days = document.get('days', [])
    for earlier, later in zip(days, days[1:]):
        origin = str(earlier.get('base_city') or '').strip()
        destination = str(later.get('base_city') or '').strip()
        if origin and destination and _name(origin) != _name(destination):
            concerns.append({'from_day': earlier['day'], 'to_day': later['day'],
                             'origin_city': origin[:100], 'destination_city': destination[:100],
                             'status': ('transfer_claim_unverified' if later.get('intercity_transfer')
                                        else 'transfer_not_specified'),
                             'note': 'Verify transport endpoints, service and transfer time externally.'})
    return concerns[:29]
