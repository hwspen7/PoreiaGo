"""External data integration without network traffic or secret credentials."""
from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest

from aligo.contracts import Evidence
from aligo.provider_planning_evidence import (route_evidence, supplier_price_evidence,
                                             verified_day_route_issues)
from aligo.providers import ProviderResult, ProviderUnavailable
from aligo.travel_intelligence import TravelSkillOrchestrator


PLACES = [
    {'name': 'First Museum', 'latitude': 40.0, 'longitude': -83.0, 'type': 'attraction'},
    {'name': 'Second Gallery', 'latitude': 40.01, 'longitude': -82.98, 'type': 'attraction'},
]


class Providers:
    def __init__(self, *, enabled=('route_matrix', 'flights', 'accommodation_offers'),
                 fail=False):
        self.enabled = set(enabled)
        self.calls = []
        self.fail = fail

    def available(self, name):
        return name in self.enabled

    async def request(self, name, **kwargs):
        self.calls.append((name, kwargs))
        if self.fail:
            raise ProviderUnavailable('test outage')
        if name == 'route_matrix':
            return ProviderResult({'sources_to_targets': [
                [{'distance': 0, 'time': 0}, {'distance': 1100, 'time': 900}],
                [{'distance': 1200, 'time': 960}, {'distance': 0, 'time': 0}]]},
                [Evidence(provider='geoapify-route-matrix',
                          url='https://example.org/provider', mode='live_route')])
        if name == 'flights':
            return ProviderResult({'mode': 'test', 'offers': [
                {'id': 'offer-1', 'amount': '125.30', 'currency': 'USD',
                 'expires_at': '2026-11-01T12:00:00Z'}]}, [])
        if name == 'accommodation_offers':
            return ProviderResult({'inventory_mode': 'sandbox', 'results': [
                {'id': 123, 'name': 'Hotel One', 'price': {
                    'book': {'total': '389.00', 'currency': 'USD'}}}]}, [])
        raise AssertionError(name)


@pytest.mark.asyncio
async def test_provider_route_matrix_becomes_source_backed_leg_evidence():
    provider = Providers(enabled=('route_matrix',))
    result = await route_evidence(provider, PLACES, {})
    assert result['status'] == 'provider_route'
    assert result['mode'] == 'walk'
    assert result['sources'][0]['provider'] == 'geoapify-route-matrix'
    assert result['legs'][0]['from'] == 'First Museum'
    assert result['legs'][0]['to'] == 'Second Gallery'
    assert result['legs'][0]['duration_minutes'] == 15
    assert provider.calls[0][1]['mode'] == 'walk'


@pytest.mark.asyncio
async def test_no_online_or_invalid_coordinates_makes_no_external_calls():
    provider = Providers(enabled=())
    assert await route_evidence(provider, PLACES, {}) == {}
    provider = Providers(enabled=('route_matrix',))
    assert await route_evidence(provider, [PLACES[0], {'name': 'Bad', 'latitude': 999,
                                                       'longitude': 25}], {}) == {}
    assert provider.calls == []


@pytest.mark.asyncio
async def test_transit_is_not_confused_with_walk_matrix():
    provider = Providers(enabled=('route_matrix',))
    assert await route_evidence(provider, PLACES, {'transport_mode': 'transit'}) == {}
    assert provider.calls == []


@pytest.mark.asyncio
async def test_outage_keeps_model_only_fallback():
    provider = Providers(enabled=('route_matrix',), fail=True)
    assert await route_evidence(provider, PLACES, {}) == {}
    assert len(provider.calls) == 1


def test_only_matching_consecutive_walking_visits_are_strictly_checked():
    evidence = {'status': 'provider_route', 'mode': 'walk', 'legs': [
        {'from': 'First Museum', 'to': 'Second Gallery', 'duration_minutes': 15,
         'distance_m': 1100}]}
    day = {'activities': [
        {'place_name': 'First Museum', 'start_time': '09:00', 'duration_minutes': 90},
        {'place_name': 'Second Gallery', 'start_time': '10:35',
         'duration_minutes': 60, 'transport_hint': 'Walk to the gallery.'}]}
    assert len(verified_day_route_issues(day, evidence)) == 1
    revised = deepcopy(day)
    revised['activities'][1]['start_time'] = '10:50'
    assert verified_day_route_issues(revised, evidence) == []
    revised['activities'][1]['start_time'] = '10:35'
    revised['activities'][1]['transport_hint'] = 'Take the metro.'
    assert verified_day_route_issues(revised, evidence) == []
    revised['activities'][1]['place_name'] = 'Unmatched stop'
    assert verified_day_route_issues(revised, evidence) == []
    revised['activities'][1]['place_name'] = 'Second Gallery'
    revised['activities'][1].pop('transport_hint')
    # Without a declared mode, a walking route cannot invalidate a car transfer.
    assert verified_day_route_issues(revised, evidence) == []


@pytest.mark.asyncio
async def test_day_context_places_verified_matrix_in_model_inputs():
    provider = Providers(enabled=('route_matrix',))
    orchestrator = TravelSkillOrchestrator(None, provider)
    result = await orchestrator.day_context(
        {'city': 'Columbus', 'places': PLACES},
        {'day': 1, 'city': 'Columbus', 'area': 'Downtown', 'theme': 'Museums'})
    assert result['verified_route_evidence']['legs'][0]['duration_minutes'] == 15
    assert result['places'][0]['name'] == 'First Museum'
    assert any(row.get('skill') == 'route-matrix-evidence' for row in orchestrator.trace)


@pytest.mark.asyncio
async def test_quotes_require_explicit_airports_dates_and_booking_city():
    provider = Providers()
    assert await supplier_price_evidence(provider, {'destination': 'New York'}) == {}
    assert not provider.calls
    result = await supplier_price_evidence(provider, {
        'origin_airport': 'CMH', 'destination_airport': 'JFK',
        'departure_date': '2026-11-15', 'travelers': 2,
        'hotel_checkin': '2026-11-15', 'hotel_checkout': '2026-11-17',
        'booking_city_id': 123456, 'rooms': 1})
    assert result['flights']['verification_level'] == 'test_offer_not_bookable'
    assert result['accommodation']['verification_level'] == 'sandbox_inventory_not_bookable'
    assert result['accommodation']['offers'][0]['price']['book']['total'] == '389.00'
    assert len(provider.calls) == 2
    assert all('apiKey' not in repr(args) for _, args in provider.calls)


@pytest.mark.asyncio
async def test_live_mode_only_labels_live_quotes_from_live_provider():
    class Live(Providers):
        async def request(self, name, **kwargs):
            result = await super().request(name, **kwargs)
            if name == 'flights':
                result.data['mode'] = 'live'
            return result
    provider = Live(enabled=('flights',))
    offers = await supplier_price_evidence(provider, {
        'origin_airport': 'CMH', 'destination_airport': 'JFK',
        'departure_date': '2026-11-15'})
    assert offers['flights']['verification_level'] == 'live_supplier_quote'
    assert 'accommodation' not in offers


@pytest.mark.asyncio
async def test_quote_provider_failure_does_not_block_planning():
    provider = Providers(fail=True)
    assert await supplier_price_evidence(provider, {
        'origin_airport': 'CMH', 'destination_airport': 'JFK',
        'departure_date': '2026-11-15'}) == {}


@pytest.mark.asyncio
async def test_market_context_goes_through_existing_orchestrator_not_new_skill():
    provider = Providers(enabled=('flights',))
    orchestrator = TravelSkillOrchestrator(None, provider)
    data = await orchestrator.market_price_context({
        'origin_airport': 'CMH', 'destination_airport': 'JFK',
        'departure_date': '2026-11-15'})
    assert data['flights']['source'] == 'duffel'
    assert orchestrator.trace[0]['skill'] == 'supplier-price-evidence'


@pytest.mark.asyncio
async def test_segmented_daily_model_receives_price_evidence_without_new_skill():
    import json
    from pathlib import Path
    from aligo.registry import SkillRegistry
    from aligo.segmented_planning import generate_segmented_trip

    class OneDayModel:
        def __init__(self):
            self.prompts = []

        async def complete(self, role, system, prompt, json_mode=False):
            data = json.loads(prompt)
            self.prompts.append(data)
            if 'allocated_days' not in data:
                return {'blueprint': {'destination': 'Osaka', 'title': 'Osaka art',
                        'summary': 'City museums', 'days': [
                            {'day': 1, 'city': 'Osaka', 'area': 'Namba',
                             'theme': 'Art and culture', 'anchors': ['Namba'],
                             'overnight': 'Osaka', 'transfer': '',
                             'day_type': 'sightseeing'}]}}
            return {'days': [{'day': 1, 'title': 'Osaka food and art', 'area_cluster': 'Namba',
                              'logistics': 'Walk between nearby venues.',
                              'weather_alternative': 'Explore an indoor museum near Namba.',
                              'activities': [
                                  {'description': 'Explore Osaka cultural heritage in Namba',
                                   'time': 'Morning', 'start_time': '10:00',
                                   'duration_minutes': 120},
                                  {'description': 'Enjoy a named Osaka food experience',
                                   'time': 'Afternoon', 'start_time': '12:30',
                                   'duration_minutes': 60},
                                  {'description': 'Explore a nearby art gallery',
                                   'time': 'Evening', 'start_time': '16:00',
                                   'duration_minutes': 60}]}]}

    provider = Providers(enabled=('flights',))
    model = OneDayModel()
    root = Path(__file__).resolve().parents[1]
    trip = await generate_segmented_trip(
        model, request='Plan a day in Osaka, including my flight.', destination='Osaka',
        days=1, fields={'days': 1, 'destination': 'Osaka',
                        'origin_airport': 'CMH', 'destination_airport': 'KIX',
                        'departure_date': '2026-11-15'}, preferences={},
        registry=SkillRegistry(root / 'plugins/skills'), providers=provider)
    assert len(trip['days']) == 1
    prompts = [p for p in model.prompts if 'allocated_days' in p]
    assert prompts
    assert prompts[0]['source_backed_supplier_offers']['flights']['verification_level'] == 'test_offer_not_bookable'
    assert [c[0] for c in provider.calls] == ['flights']
