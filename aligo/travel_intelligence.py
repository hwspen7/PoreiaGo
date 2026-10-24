"""Bounded orchestration of specialist travel Skills for day-by-day planning.

LLMs own trip-level choices. Skills can provide *candidates* and deterministic
schedule checking; they do not certify prices, routes or opening hours.
Offline runs incur no external requests and preserve the model-only fallback.
"""
from __future__ import annotations

import asyncio
import os
import re
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

from .providers import ProviderUnavailable
from .provider_planning_evidence import (route_evidence, supplier_price_evidence,
                                        verified_day_route_issues)


class TravelSkillOrchestrator:
    """A bounded bridge to manifest-registered Skills with explicit provenance."""

    def __init__(self, registry: Any, providers: Any, *, max_cities: int | None = None):
        self.registry = registry
        self.providers = providers
        self.max_cities = max(1, min(max_cities if max_cities is not None else
                                     self._budget('ALIGO_MAX_DISCOVERY_CITIES', 4, 1, 12), 12))
        self.city_candidate_limit = self._budget('ALIGO_CITY_CANDIDATE_LIMIT', 12, 6, 24)
        self.day_candidate_limit = self._budget('ALIGO_DAY_CANDIDATE_LIMIT', 7, 3, 18)
        self.skill_budget = self._budget('ALIGO_PROFESSIONAL_SKILL_BUDGET', 5, 1, 9)
        self.results_per_skill = self._budget('ALIGO_PROFESSIONAL_RESULTS_PER_SKILL', 4, 1, 8)
        self.trace: list[dict] = []

    @staticmethod
    def _budget(name: str, default: int, low: int, high: int) -> int:
        try:
            value = int(os.getenv(name, str(default)))
        except (TypeError, ValueError):
            value = default
        return max(low, min(value, high))

    def _installed(self, name: str) -> bool:
        mode = os.getenv('ALIGO_ITINERARY_SKILL_MODE', 'eighteen').strip().lower()
        if mode == 'none':
            return False
        if mode == 'nine' and name in {
            'neighborhood-intelligence', 'opening-hours-intelligence',
            'ticket-entry-planning', 'local-events-discovery',
            'culinary-specialty-discovery', 'seasonal-experience-intelligence',
            'accessibility-suitability', 'day-trip-discovery',
            'itinerary-feasibility-audit'}:
            return False
        if not self.registry:
            return False
        spec = self.registry.get(name)
        return bool(spec and spec.release_state == 'implemented' and spec.entrypoint)

    def _online(self) -> bool:
        method = getattr(self.providers, 'available', None)
        if not callable(method):
            return False
        try:
            return bool(method('geocode') and method('places'))
        except (KeyError, ValueError, TypeError):
            return False

    def _ctx(self, city: str, request: str, fields: dict) -> SimpleNamespace:
        return SimpleNamespace(fields={**fields, 'destination': city},
                               query=request, providers=self.providers, store=None,
                               models=None, shared={}, user_id='planning', session_id='planning')

    async def candidate_context(self, allocations: list[dict], *, request: str,
                                fields: dict) -> dict[str, dict]:
        """Retrieve place/dining candidates once per city, with no unlimited fanout."""
        if not self._online() or not self._installed('place-discovery'):
            return {}
        cities: list[str] = []
        for row in allocations:
            city = str(row.get('city') or '').strip()
            if city and city.casefold() not in {name.casefold() for name in cities}:
                cities.append(city)
            if len(cities) >= self.max_cities:
                break
        result: dict[str, dict] = {}
        food_relevant = bool(re.search(r'\b(?:food|culinary|dining|restaurant|cuisine|eat|meal|lunch|dinner|breakfast)\b',
                                       request, re.I))
        # Explicit preferences are language-neutral signals; the user's last
        # message need not repeat culinary or cultural interests in English.
        food_relevant |= any(bool(fields.get(k)) for k in
                             ('cuisine', 'food_preferences', 'dietary_requirements'))
        food_relevant |= any(re.search(r'\b(?:food|cuisine|dining|market|culinary)\b',
                                        str(row.get('theme') or ''), re.I)
                             for row in allocations)
        user_interests = fields.get('interests') or fields.get('travel_interests') or []
        if isinstance(user_interests, str):
            user_interests = [user_interests]
        if not isinstance(user_interests, (list, tuple)):
            user_interests = []
        cultural_relevant = bool(re.search(
            r'\b(?:craft|workshop|artisan|ceramics|textile|heritage|handmade)\b',
            request + ' ' + ' '.join(str(row.get('theme') or '') for row in allocations)
            + ' ' + ' '.join(str(v) for v in user_interests), re.I))
        cultural_relevant |= any(bool(fields.get(k)) for k in
                                 ('cultural_interests', 'experience_preferences'))
        semaphore = asyncio.Semaphore(2)

        async def fetch_city(city: str):
            async with semaphore:
                try:
                    return await asyncio.wait_for(lookup_city(city), timeout=36)
                except asyncio.TimeoutError:
                    self.trace.append({'skill': 'city-discovery', 'city': city,
                                       'status': 'timeout'})
                    return None

        async def lookup_city(city: str):
            context = self._ctx(city, request, fields)
            try:
                geocoded = await asyncio.wait_for(
                    self.providers.request('geocode', place=city), timeout=20)
                matches = [row for row in (geocoded.data.get('results') or [])
                           if isinstance(row, dict) and
                           str(row.get('name', '')).casefold() == city.casefold()]
                # Geocoding is discovery, not city identity. Refuse a guessed
                # location if several countries share the same city name.
                desired = str(fields.get('country') or fields.get('destination') or '').casefold()
                by_country = [row for row in matches if str(row.get('country', '')).casefold() == desired]
                if by_country:
                    matches = by_country
                if not matches or len({str(row.get('country', '')).casefold()
                                       for row in matches}) > 1:
                    self.trace.append({'skill': 'geocode', 'city': city,
                                       'status': 'ambiguous_or_missing'})
                    return None
                coordinate = {'latitude': float(matches[0]['latitude']),
                              'longitude': float(matches[0]['longitude'])}
            except (ProviderUnavailable, asyncio.TimeoutError, ValueError, KeyError, TypeError) as exc:
                self.trace.append({'skill': 'geocode', 'city': city,
                                   'status': 'error', 'reason': type(exc).__name__})
                return None
            # Give the specialist actual trip preferences rather than merely a
            # city coordinate. The Skill's ranking must affect what the daily
            # planner receives instead of being discarded after discovery.
            interests = fields.get('interests') or fields.get('travel_interests') or []
            if isinstance(interests, str):
                interests = [interests]
            if not isinstance(interests, list):
                interests = []
            excluded = fields.get('avoid_places') or fields.get('excluded_places') or []
            if isinstance(excluded, str):
                excluded = [excluded]
            if not isinstance(excluded, list):
                excluded = []
            selections = [('place-discovery', {**coordinate, 'kind': 'tourism',
                         'interests': interests[:8], 'exclude_names': excluded[:40]})]
            if food_relevant and self._installed('restaurant-discovery'):
                selections.append(('restaurant-discovery', {**coordinate, 'city': city,
                                   'cuisine': fields.get('cuisine') or '',
                                   'exclude_names': excluded[:40] +
                                   (fields.get('excluded_restaurants') or [] if isinstance(
                                    fields.get('excluded_restaurants'), list) else [])[:40]}))
            combined: list[dict] = []
            tourism_candidates: list[dict] = []
            for name, args in selections[:2]:
                try:
                    output = await asyncio.wait_for(self.registry.run(name, context, args), timeout=15)
                except (asyncio.TimeoutError, ProviderUnavailable, ValueError, KeyError, RuntimeError) as exc:
                    self.trace.append({'skill': name, 'city': city, 'status': 'error',
                                       'reason': type(exc).__name__})
                    continue
                self.trace.append({'skill': name, 'city': city, 'status': output.status,
                                   'candidates': len(output.data.get('results') or [])})
                if name == 'place-discovery':
                    tourism_candidates = [row for row in (output.data.get('results') or [])[:20]
                                          if isinstance(row, dict) and row.get('name')]
                for candidate in (output.data.get('results') or [])[:self.city_candidate_limit]:
                    if not isinstance(candidate, dict) or not isinstance(candidate.get('name'), str):
                        continue
                    name_text = candidate['name'].strip()
                    if not name_text:
                        continue
                    record = {'name': name_text[:130],
                              'type': 'restaurant' if name == 'restaurant-discovery' else 'attraction',
                              'verification_level': 'map_candidate',
                              'source_provider': (output.sources[0].provider if output.sources else 'unverified'),
                              'observed_at': (output.sources[0].observed_at if output.sources else None)}
                    for key in ('latitude', 'longitude', 'osm_id', 'osm_type', 'place_id', 'matching_score', 'area', 'neighborhood', 'source_url', 'weekly_hours', 'opening_hours', 'reservation_required', 'booking_url', 'ticket_price', 'currency', 'entry_windows', 'cuisine', 'specialties', 'season_months', 'accessibility', 'category'):
                        if key in candidate:
                            record[key] = candidate[key]
                    # Preserve locality evidence for daily neighborhood ranking.
                    # Limit provider text; venue metadata is untrusted input.
                    if isinstance(candidate.get('address'), str):
                        record['address'] = candidate['address'][:160]
                    if not any(row['name'].casefold() == name_text.casefold()
                               and row['type'] == record['type'] for row in combined):
                        combined.append(record)
            experiences: list[dict] = []
            if cultural_relevant and tourism_candidates and self._installed('cultural-experience-search'):
                try:
                    output = await asyncio.wait_for(self.registry.run(
                        'cultural-experience-search', context,
                        {'candidates': tourism_candidates, 'limit': 6}), timeout=8)
                    experiences = (output.data.get('results') or [])[:6]
                    self.trace.append({'skill': 'cultural-experience-search', 'city': city,
                                       'status': output.status, 'candidates': len(experiences)})
                except (asyncio.TimeoutError, KeyError, ValueError, TypeError) as exc:
                    self.trace.append({'skill': 'cultural-experience-search', 'city': city,
                                       'status': 'error', 'reason': type(exc).__name__})
            if combined:
                return (city.casefold(), {
                    'city': city, 'places': combined[:self.city_candidate_limit], 'experiences': experiences,
                    'note': ('These are mapped venue candidates only. They do not establish '
                             'opening hours, prices, routes, reservations or accessibility.')})
            return None

        pending = {asyncio.create_task(fetch_city(city)) for city in cities}
        done, unfinished = await asyncio.wait(pending, timeout=45)
        for task in unfinished:
            task.cancel()
        if unfinished:
            await asyncio.gather(*unfinished, return_exceptions=True)
            self.trace.append({'skill': 'city-discovery', 'status': 'partial_timeout',
                               'unfinished_cities': len(unfinished)})
        for task in done:
            if task.cancelled():
                continue
            try:
                item = task.result()
            except (ProviderUnavailable, ValueError, KeyError, TypeError) as exc:
                self.trace.append({'skill': 'city-discovery', 'status': 'error',
                                   'reason': type(exc).__name__})
                continue
            if item:
                result[item[0]] = item[1]
        return result

    async def day_context(self, city_data: dict, allocation: dict, *,
                          previous_days: list[dict] | None = None,
                          future_anchors: list[str] | None = None,
                          request: str = '', fields: dict | None = None) -> dict:
        """Select fresh, relevant daily venues and provide bounded proximity advice.

        City discovery is cached, but every day must have a new selection. The
        model remains responsible for choosing real visits and checking details.
        """
        if not city_data:
            fields = fields or {}
            user_candidates = fields.get('venue_candidates') or []
            if isinstance(user_candidates, list) and user_candidates:
                extra = await self.professional_day_context(user_candidates[:20], allocation,
                                                            request=request, fields=fields)
                if extra:
                    return {'professional_evidence': extra,
                            'note': 'User-supplied venue evidence is not independently verified.'}
            return {}
        previous_days = previous_days or []
        future_anchors = future_anchors or []
        current_anchors = [str(x) for x in allocation.get('anchors') or []]
        previous_names: list[str] = []
        previous_text: list[str] = []
        for day in previous_days:
            for activity in day.get('activities') or []:
                if not isinstance(activity, dict):
                    continue
                previous_text.append(str(activity.get('description') or ''))
                for key in ('place_name', 'place', 'venue_name'):
                    if activity.get(key):
                        previous_names.append(str(activity[key]))
        previous_normalized = ' '.join(re.findall(r'[^\W_]+',
                                ' '.join(previous_text).casefold()))
        previous_exact = {self._place_key(x) for x in previous_names}
        future = [self._place_key(x) for x in future_anchors]
        current = [self._place_key(x) for x in current_anchors]

        def includes(anchor: str, key: str) -> bool:
            return bool(key and re.search(r'(?<!\w)' + re.escape(key) +
                                         r'(?!\w)', anchor))

        def excluded(row: dict) -> bool:
            key = self._place_key(row.get('name'))
            if not key:
                return True
            if key in previous_exact or (len(key) >= 6 and includes(previous_normalized, key)):
                return True
            # An anchor reserved for another day should not be suggested for
            # today, except when today's blueprint explicitly owns it.
            return any(includes(anchor, key) for anchor in future) and not any(
                includes(anchor, key) for anchor in current)

        query = ' '.join(str(allocation.get(k) or '') for k in ('area', 'theme'))
        terms = set(re.findall(r'[^\W_]{4,}', query.casefold()))
        area = self._place_key(allocation.get('area'))

        def score(row: dict) -> tuple[int, str]:
            key = self._place_key(row.get('name'))
            raw = row.get('matching_score') or 0
            try:
                matching = max(0, min(int(raw), 10))
            except (TypeError, ValueError, OverflowError):
                matching = 0
            anchored = int(any(includes(anchor, key) for anchor in current))
            near_area = int(bool(area and area in self._place_key(
                ' '.join(str(row.get(k) or '') for k in ('name', 'address', 'area')))))
            name_terms = set(re.findall(r'[^\W_]{4,}', key))
            value = 15 * anchored + 4 * near_area + len(terms & name_terms) + matching
            return (-value, key)

        places = sorted((row for row in city_data.get('places') or []
                         if isinstance(row, dict) and row.get('name') and not excluded(row)),
                        key=score)
        selected = places[:self.day_candidate_limit]
        if not any(row.get('type') == 'restaurant' for row in selected):
            restaurant = next((row for row in places if row.get('type') == 'restaurant'), None)
            if restaurant and restaurant not in selected:
                selected = selected[:self.day_candidate_limit - 1] + [restaurant]
        experiences = sorted((row for row in city_data.get('experiences') or []
                              if isinstance(row, dict) and row.get('name') and not excluded(row)),
                             key=score)[:3]

        def compact(row: dict) -> dict:
            return {key: row[key] for key in
                    ('name', 'type', 'verification_level', 'place_id', 'matching_score',
                     'address', 'area', 'neighborhood', 'latitude', 'longitude', 'source_url',
                     'source_provider', 'observed_at', 'specific_session_identified', 'booking_status') if key in row}

        # Preserve bounded, untrusted venue identity evidence for independent
        # comparison with the finalized itinerary. The trace is not a venue
        # certification and must never be described as live verification.
        self.trace.append({
            'skill': 'day-candidate-evidence',
            'city': str(allocation.get('city') or '')[:80],
            'day': allocation.get('day'),
            'kind': 'candidate_evidence',
            'candidate_places': [{key: row[key] for key in
                                  ('name', 'area', 'neighborhood', 'place_id',
                                   'latitude', 'longitude', 'source_url', 'address') if key in row}
                                 for row in (selected + experiences)[:10]],
        })
        output = {'city': city_data.get('city', allocation.get('city')),
                  'places': [compact(row) for row in selected],
                  'experiences': [compact(row) for row in experiences],
                  'note': ('Unverified map candidates only. No opening hours, '
                           'prices, transit times or bookings are established.')}
        mapped = [row for row in selected if row.get('type') != 'restaurant'
                  and all(isinstance(row.get(k), (int, float)) and not isinstance(row.get(k), bool)
                          for k in ('latitude', 'longitude'))][:5]
        if len(mapped) >= 2 and self._installed('multi-stop-route-optimizer'):
            try:
                response = await asyncio.wait_for(self.registry.run(
                    'multi-stop-route-optimizer', self._ctx(str(allocation.get('city') or ''), '', {}),
                    {'waypoints': [{'name': row['name'], 'latitude': row['latitude'],
                                    'longitude': row['longitude'],
                                    'area': row.get('area', '')} for row in mapped],
                     'max_leg_km': 6, 'max_total_km': 16}), timeout=4)
                stops = response.data.get('ordered_stops') or []
                if len(stops) == len(mapped):
                    output['proximity_hint'] = {
                        'suggested_order': [row['name'] for row in stops],
                        'straight_line_total_km': response.data.get('total_km_straight_line'),
                        'warnings': (response.data.get('planning_warnings') or [])[:2],
                        'caveat': ('Proximity ordering only; do not present this as '
                                   'a verified itinerary, walking time or transit route.')}
                self.trace.append({'skill': 'multi-stop-route-optimizer',
                                   'city': allocation.get('city'), 'day': allocation.get('day'),
                                   'status': response.status, 'stops': len(stops)})
            except (asyncio.TimeoutError, KeyError, TypeError, ValueError, RuntimeError) as exc:
                self.trace.append({'skill': 'multi-stop-route-optimizer',
                                   'city': allocation.get('city'), 'status': 'error',
                                   'reason': type(exc).__name__})
        # Check a bounded real road-network matrix when Geoapify is configured.
        # This evidence goes into the daily model prompt, not into a new Skill.
        # Without the provider, the earlier proximity-only hint remains intact.
        verified_routes = await route_evidence(self.providers, mapped, fields or {})
        if verified_routes:
            output['verified_route_evidence'] = verified_routes
            self.trace.append({'skill': 'route-matrix-evidence',
                               'city': allocation.get('city'), 'day': allocation.get('day'),
                               'status': 'ok', 'legs': len(verified_routes['legs']),
                               'mode': verified_routes['mode']})
        # The everyday provider route must enrich the same selected candidates
        # before serializing them for the Daily Planner. Keep raw metadata for
        # specialist checks; compact model-facing records are not sufficient.
        specialist_candidates = selected + experiences
        extra = await self.professional_day_context(
            specialist_candidates, allocation, request=request, fields=fields or {})
        if extra:
            output['professional_evidence'] = extra
        return output

    async def professional_day_context(self, candidates: list[dict], allocation: dict,
                                       *, request: str = '', fields: dict | None = None) -> dict:
        """Route a bounded subset of optional skills using explicit trip evidence."""
        fields = fields or {}
        city = str(allocation.get('city') or '')
        text = ' '.join((request, str(allocation.get('theme') or ''),
                         str(allocation.get('area') or ''))).casefold()
        enriched = [row for row in candidates[:self.city_candidate_limit] if isinstance(row, dict) and row.get('name')]
        planned: list[tuple[str, dict]] = []
        if any(row.get('area') or row.get('neighborhood') for row in enriched):
            planned.append(('neighborhood-intelligence', {'candidates': enriched,
                           'interests': fields.get('interests') if isinstance(fields.get('interests'), list) else [],
                           'limit': 4}))
        if any(row.get('weekly_hours') or row.get('opening_hours') for row in enriched):
            planned.append(('opening-hours-intelligence', {'candidates': enriched,
                           'visit_date': str(allocation.get('date') or fields.get('start_date') or '')[:10],
                           'limit': 8}))
        if any(row.get(k) is not None for row in enriched for k in
               ('reservation_required', 'booking_url', 'ticket_price', 'entry_windows')):
            planned.append(('ticket-entry-planning', {'candidates': enriched, 'limit': 6}))
        event_rows = fields.get('event_candidates') or fields.get('local_events') or []
        event_capable = False
        available = getattr(self.providers, 'available', None)
        if callable(available):
            try:
                event_capable = bool(available('events'))
            except (KeyError, ValueError, TypeError):
                pass
        if isinstance(event_rows, list) and (event_rows or (
                event_capable and re.search(r'\b(?:festival|concert|event|performance|exhibition)\b', text))):
            planned.append(('local-events-discovery', {
                'events': event_rows[:45], 'city': city,
                'start_date': str(fields.get('start_date') or '')[:10],
                'end_date': str(fields.get('end_date') or '')[:10], 'limit': 5}))
        if re.search(r'\b(?:food|eat|dining|restaurant|culinary|cuisine|lunch|dinner|market)\b', text) or fields.get('cuisine'):
            food = [row for row in enriched if row.get('type') == 'restaurant' or row.get('cuisine')]
            if food:
                planned.append(('culinary-specialty-discovery', {
                    'candidates': food, 'interests': [str(fields.get('cuisine') or '')], 'limit': 5}))
        month = fields.get('month') or fields.get('travel_month')
        if not month:
            date_value = str(allocation.get('date') or fields.get('start_date') or '')
            if re.fullmatch(r'\d{4}-\d{2}-\d{2}', date_value):
                month = int(date_value[5:7])
        seasonal = [row for row in enriched if isinstance(row.get('season_months'), list)]
        if seasonal and month:
            planned.append(('seasonal-experience-intelligence', {
                'candidates': seasonal, 'month': month, 'limit': 5}))
        needs = fields.get('accessibility_requirements') or fields.get('mobility_requirements') or []
        if isinstance(needs, str) and needs.strip():
            needs = [needs.strip()]
        if isinstance(needs, list) and needs:
            planned.append(('accessibility-suitability', {'candidates': enriched,
                                                          'requirements': needs[:10], 'limit': 7}))
        day_trips = fields.get('day_trip_candidates') or []
        if isinstance(day_trips, list) and day_trips and (
                'day trip' in text or allocation.get('day_type') == 'day_trip'):
            planned.append(('day-trip-discovery', {'candidates': day_trips[:35],
                                                    'origin': city, 'limit': 5}))
        results: dict[str, dict] = {}
        # Enforce hard user constraints before optional enrichment. Do not drop
        # mobility requirements due to a fixed feature-discovery order.
        priority = {
            'accessibility-suitability': 100,
            'opening-hours-intelligence': 76,
            'ticket-entry-planning': 70,
            'seasonal-experience-intelligence': 63,
            'local-events-discovery': 60,
            'day-trip-discovery': 58,
            'culinary-specialty-discovery': 45,
            'neighborhood-intelligence': 30,
        }
        explicit = {
            'accessibility-suitability': bool(needs),
            'local-events-discovery': bool(fields.get('event_candidates') or fields.get('local_events')),
            'day-trip-discovery': bool(fields.get('day_trip_candidates')),
            'culinary-specialty-discovery': bool(fields.get('cuisine') or fields.get('food_preferences')),
            'seasonal-experience-intelligence': bool(fields.get('month') or fields.get('travel_month')),
        }
        available_calls = [(name, arguments) for name, arguments in planned if self._installed(name)]
        available_calls.sort(key=lambda row: -(priority.get(row[0], 0) +
                                                (25 if explicit.get(row[0]) else 0)))
        budget = self.skill_budget
        # Per-request budgets are opt-in and clamped to the nine specialists.
        if isinstance(fields.get('professional_skill_budget'), int) and not isinstance(fields['professional_skill_budget'], bool):
            budget = max(1, min(fields['professional_skill_budget'], 9))
        skipped = available_calls[budget:]
        if skipped:
            self.trace.append({'skill': 'professional-skill-routing', 'city': city,
                               'day': allocation.get('day'),
                               'selected': [name for name, _ in available_calls[:budget]],
                               'deferred': [name for name, _ in skipped]})
        for name, arguments in available_calls[:budget]:
            if not self._installed(name):
                continue
            try:
                response = await asyncio.wait_for(
                    self.registry.run(name, self._ctx(city, request, fields), arguments), timeout=5)
                entries = response.data.get('results') or []
                if isinstance(entries, list) and entries:
                    results[name] = {'status': response.status, 'results': entries[:self.results_per_skill],
                                     'limitations': response.limitations[:2]}
                self.trace.append({'skill': name, 'city': city, 'day': allocation.get('day'),
                                   'status': response.status,
                                   'candidates': len(entries) if isinstance(entries, list) else 0})
            except (asyncio.TimeoutError, ValueError, KeyError, TypeError, RuntimeError) as exc:
                self.trace.append({'skill': name, 'city': city, 'status': 'error',
                                   'reason': type(exc).__name__})
        return results

    @staticmethod
    def _place_key(value: Any) -> str:
        return ' '.join(re.findall(r'[^\W_]+', str(value or '').casefold()))

    async def budget_context(self, fields: dict, days: int) -> dict:
        """Supply a numerical allocation only when traveler budget is explicit."""
        if not self._installed('trip-budget-allocation'):
            return {}
        amount = fields.get('budget')
        if fields.get('currency_ambiguous') and not fields.get('currency'):
            return {'status': 'currency_unresolved',
                    'currency_candidates': fields.get('currency_candidates', [])}
        if isinstance(amount, bool) or not isinstance(amount, (int, float)) or amount <= 0:
            return {}
        args = {'total': amount, 'days': days,
                'travelers': fields.get('travelers', 1),
                'currency': fields.get('currency', 'USD'),
                'basis': fields.get('budget_basis', 'group_total')}
        try:
            output = await asyncio.wait_for(self.registry.run(
                'trip-budget-allocation', self._ctx('', '', fields), args), timeout=5)
        except (asyncio.TimeoutError, ValueError, KeyError, TypeError) as exc:
            self.trace.append({'skill': 'trip-budget-allocation', 'status': 'error',
                               'reason': type(exc).__name__})
            return {}
        self.trace.append({'skill': 'trip-budget-allocation', 'status': output.status})
        if output.status != 'ok':
            return {}
        return {'allocation': output.data.get('allocation'),
                'currency': output.data.get('currency'),
                'basis': output.data.get('input_basis'),
                'daily_average': output.data.get('daily_average'),
                'verification_level': 'allocation_only'}

    async def market_price_context(self, fields: dict) -> dict:
        """Get optional source-labelled supplier offers for explicit search parameters."""
        evidence = await supplier_price_evidence(self.providers, fields)
        for kind, details in evidence.items():
            self.trace.append({'skill': 'supplier-price-evidence', 'kind': kind,
                               'status': 'ok', 'environment': details['environment'],
                               'offers': len(details['offers'])})
        return evidence

    @staticmethod
    def verified_transport_issues(day: dict, day_context: dict) -> list[str]:
        return verified_day_route_issues(day,
                                        (day_context or {}).get('verified_route_evidence') or {})

    async def schedule_issues(self, day: dict) -> list[str]:
        """Run an existing Skill against explicit HH:MM + duration data only."""
        if self._installed('itinerary-feasibility-audit'):
            try:
                audit = await asyncio.wait_for(self.registry.run(
                    'itinerary-feasibility-audit', self._ctx('', '', {}),
                    {'activities': (day.get('activities') or [])[:60]}), timeout=4)
                self.trace.append({'skill': 'itinerary-feasibility-audit',
                                   'status': audit.status,
                                   'risks': len(audit.data.get('risks') or [])})
            except (asyncio.TimeoutError, ValueError, KeyError, TypeError, RuntimeError) as exc:
                self.trace.append({'skill': 'itinerary-feasibility-audit',
                                   'status': 'error', 'reason': type(exc).__name__})
        if not self._installed('schedule-conflict-validation'):
            return []
        events: list[dict] = []
        anchor = datetime(2000, 1, 1)
        for idx, activity in enumerate(day.get('activities') or []):
            time_text = str(activity.get('start_time') or activity.get('time') or '').strip()
            minutes = activity.get('duration_minutes')
            if (not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', time_text)
                    or not isinstance(minutes, int) or isinstance(minutes, bool) or minutes <= 0):
                continue
            hours, mins = map(int, time_text.split(':'))
            start = anchor + timedelta(hours=hours, minutes=mins)
            end = start + timedelta(minutes=minutes)
            event = {'name': f'Activity {idx + 1}', 'start': start.isoformat(),
                     'end': end.isoformat()}
            # The next activity owns incoming travel and safety buffers. This
            # metadata must reach the Skill; checking clock overlap alone is
            # not sufficient to establish a workable schedule.
            for key in ('transfer_minutes', 'buffer_minutes'):
                value = activity.get(key, 0)
                if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 360:
                    event[key] = value
            events.append(event)
        if len(events) < 2:
            return []
        try:
            output = await self.registry.run('schedule-conflict-validation',
                                             self._ctx('', '', {}), {'events': events})
        except (KeyError, ValueError, TypeError) as exc:
            self.trace.append({'skill': 'schedule-conflict-validation',
                               'status': 'error', 'reason': type(exc).__name__})
            return []
        collisions = output.data.get('conflicts') or []
        self.trace.append({'skill': 'schedule-conflict-validation',
                           'status': output.status, 'conflicts': len(collisions)})
        issues = []
        for item in collisions[:4]:
            earlier = item.get('previous', 'earlier activity')
            later = item.get('next', 'later activity')
            if item.get('reason') == 'insufficient_transfer_buffer':
                issues.append(f'Insufficient transfer/buffer between {earlier} and {later}: '
                              f"need {item.get('required_buffer_minutes')} minutes; "
                              f"available {item.get('actual_gap_minutes')} minutes.")
            else:
                issues.append(f'Timed activities overlap ({earlier} and {later}). '
                              'Adjust start time and duration.')
        return issues
