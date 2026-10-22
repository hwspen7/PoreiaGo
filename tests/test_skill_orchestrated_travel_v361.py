"""Bounded, source-honest Skill Orchestration and daily quality gates."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from aligo.contracts import Evidence
from aligo.providers import ProviderResult, ProviderUnavailable
from aligo.registry import SkillRegistry
from aligo.segmented_planning import generate_segmented_trip
from aligo.travel_intelligence import TravelSkillOrchestrator

ROOT = Path(__file__).resolve().parents[1]


def registry():
    return SkillRegistry(ROOT / 'plugins/skills')


def context(**kwargs):
    return SimpleNamespace(fields={}, providers=kwargs.get('providers'),
                           query='Plan a trip.', shared={})


@pytest.mark.parametrize('name', [
    'restaurant-discovery', 'place-detail-verification', 'schedule-conflict-validation'])
def test_specialist_skill_is_executable(name):
    spec = registry().get(name)
    assert spec.release_state == 'implemented'
    assert spec.entrypoint == 'handler.py:run'


@pytest.mark.asyncio
async def test_places_match_is_candidate_only_and_missing_is_not_nonexistent():
    reg = registry()
    args = {'place': 'Craft Museum', 'candidates': [
        {'name': 'Craft Museum', 'osm_id': 123, 'latitude': 33.2, 'longitude': 130.4}]}
    matching = await reg.run('place-detail-verification', context(), args)
    assert matching.data['verification_level'] == 'candidate_match'
    assert matching.status == 'partial'
    missing = await reg.run('place-detail-verification', context(), {'place': 'Another Museum'})
    assert missing.data['verification_level'] == 'unverified'
    assert not missing.data.get('nonexistent', False)


@pytest.mark.skip(reason="Legacy utility Skill retired from itinerary-first catalog")
@pytest.mark.asyncio
async def test_url_or_provider_label_does_not_verify_a_fact():
    evidence = [{'provider': 'official', 'url': 'https://example.org/pricing',
                 'observed_at': '2020-01-01'}]
    checked = await registry().run('source-verification', context(),
                                   {'sources': evidence, 'claim': 'Ticket costs 2000 JPY'})
    assert checked.data['records'][0]['provenance_present']
    assert checked.data['claim_status'] == 'verification_pending'
    assert checked.status == 'partial'


@pytest.mark.skip(reason="Legacy utility Skill retired from itinerary-first catalog")
@pytest.mark.asyncio
async def test_cost_skill_never_combines_currencies_or_group_bases():
    items = [{'amount': 1000, 'currency': 'JPY', 'basis': 'per_person'},
             {'amount': 2500, 'currency': 'JPY', 'basis': 'per_person'},
             {'amount': 500, 'currency': 'JPY', 'basis': 'group_total'},
             {'amount': 30, 'currency': 'USD', 'basis': 'per_person'},
             {'amount': -12, 'currency': 'JPY', 'basis': 'per_person'}]
    r = await registry().run('travel-cost-estimation', context(), {'items': items})
    assert len(r.data['totals']) == 3
    assert next(x['amount'] for x in r.data['totals'] if x['currency'] == 'JPY' and
                x['basis'] == 'per_person') == 3500
    assert r.data['invalid_items'] == 1
    assert r.data['verification_level'] == 'user_or_model_estimate'


@pytest.mark.skip(reason="Legacy utility Skill retired from itinerary-first catalog")
@pytest.mark.asyncio
async def test_airport_options_ranked_only_from_supplied_data():
    r = await registry().run('airport-transfer', context(), {
        'origin': 'KIX', 'destination': 'Namba', 'route_options': [
            {'mode': 'rail', 'destination': 'Tennoji', 'transfers': 1},
            {'mode': 'rail', 'destination': 'Namba', 'transfers': 0}]})
    assert r.status == 'partial'
    assert r.data['route_options'][0]['destination'] == 'Namba'
    missing = await registry().run('airport-transfer', context(),
                                   {'origin': 'KIX', 'destination': 'Namba'})
    assert missing.data['route_options'] == []


class MockProviders:
    def __init__(self, enabled=True):
        self.enabled = enabled
        self.calls = []

    def available(self, name):
        return self.enabled

    async def request(self, capability, **kwargs):
        self.calls.append((capability, kwargs))
        if capability == 'geocode':
            return ProviderResult({'results': [{'name': 'Osaka', 'country': 'Japan',
                                                'latitude': 34.67, 'longitude': 135.5}]},
                                  [Evidence(provider='mock-geocode')])
        if capability == 'places':
            kind = kwargs['kind']
            name = 'Osaka Cultural Museum' if kind == 'tourism' else 'Mapped Dining Venue'
            return ProviderResult({'results': [{'name': name, 'latitude': 34.67,
                                                'longitude': 135.5, 'osm_id': 6}]},
                                  [Evidence(provider='mock-places')])
        raise ProviderUnavailable('Unsupported')


@pytest.mark.asyncio
async def test_city_enrichment_is_bounded_and_reuses_one_geocode():
    providers = MockProviders()
    controller = TravelSkillOrchestrator(registry(), providers)
    allocation = [{'city': 'Osaka', 'theme': 'regional food'},
                  {'city': 'Osaka', 'theme': 'culture'}]
    data = await controller.candidate_context(allocation,
                                              request='Enjoy food and culture in Osaka.',
                                              fields={'destination': 'Japan'})
    assert 'osaka' in data
    assert len(data['osaka']['places']) == 2
    assert all(item['verification_level'] == 'map_candidate'
               for item in data['osaka']['places'])
    assert [call[0] for call in providers.calls].count('geocode') == 1
    assert [call[0] for call in providers.calls].count('places') == 2
    assert {row['skill'] for row in controller.trace} == {
        'place-discovery', 'restaurant-discovery'}


@pytest.mark.asyncio
async def test_offline_controller_makes_no_provider_calls():
    providers = MockProviders(enabled=False)
    controller = TravelSkillOrchestrator(registry(), providers)
    assert await controller.candidate_context([{'city': 'Osaka'}], request='Food trip',
                                              fields={}) == {}
    assert providers.calls == []


@pytest.mark.asyncio
async def test_time_overlap_uses_existing_schedule_validation_skill():
    controller = TravelSkillOrchestrator(registry(), None)
    bad = {'activities': [
        {'start_time': '10:00', 'duration_minutes': 120},
        {'start_time': '11:00', 'duration_minutes': 60}]}
    issues = await controller.schedule_issues(bad)
    assert len(issues) == 1 and 'overlap' in issues[0].lower()
    assert await controller.schedule_issues({'activities': [
        {'start_time': '10:00', 'duration_minutes': 60},
        {'start_time': '11:30', 'duration_minutes': 60}]}) == []


class OneDayModel:
    def __init__(self):
        self.calls = []

    async def complete(self, role, system, prompt, json_mode=False):
        data = json.loads(prompt)
        self.calls.append(data)
        if 'allocated_days' not in data:
            return {'blueprint': {'destination': 'Osaka', 'title': 'Osaka culture',
                    'summary': 'Food and architecture', 'days': [
                        {'day': 1, 'city': 'Osaka', 'area': 'Namba',
                         'theme': 'Local food', 'anchors': ['Namba'],
                         'overnight': 'Osaka', 'transfer': '', 'day_type': 'sightseeing'}]}}
        bad = len([call for call in self.calls if 'allocated_days' in call]) == 1
        return {'days': [{'day': 1, 'title': 'Osaka food and art', 'area_cluster': 'Namba',
                          'logistics': 'Walk between nearby venues.',
                          'weather_alternative': 'Explore an indoor museum near Namba.',
                          'activities': [
                              {'description': 'Explore Osaka cultural heritage in Namba',
                               'time': 'Morning', 'start_time': '10:00',
                               'duration_minutes': 120},
                              {'description': 'Enjoy a named Osaka food experience',
                               'time': 'Afternoon', 'start_time': '11:00' if bad else '12:30',
                               'duration_minutes': 60},
                              {'description': 'Explore a nearby art gallery',
                               'time': 'Evening', 'start_time': '16:00',
                               'duration_minutes': 60}]}]}


@pytest.mark.asyncio
async def test_planner_consumes_skill_candidates_and_repairs_schedule_overlap():
    model = OneDayModel()
    result = await generate_segmented_trip(model, request='Plan one day in Osaka with food.',
                                           destination='Osaka', days=1,
                                           fields={'days': 1, 'destination': 'Osaka'},
                                           preferences={}, registry=registry(),
                                           providers=MockProviders())
    assert len(result['days']) == 1
    daily = [row for row in model.calls if 'allocated_days' in row]
    assert len(daily) == 2
    assert len(daily[0]['specialist_skill_candidates']['places']) == 2
    assert 'overlap' in str(daily[1]['revision_issues']).lower()
    assert result['days'][0]['activities'][1]['start_time'] == '12:30'

@pytest.mark.asyncio
async def test_ambiguous_city_skips_external_evidence_instead_of_guessing():
    class Ambiguous(MockProviders):
        async def request(self, capability, **kwargs):
            if capability == 'geocode':
                self.calls.append((capability, kwargs))
                return ProviderResult({'results': [
                    {'name': 'Springfield', 'country': 'United States',
                     'latitude': 39.7, 'longitude': -89.6},
                    {'name': 'Springfield', 'country': 'Australia',
                     'latitude': -27.6, 'longitude': 152.9}]}, [])
            raise AssertionError('Ambiguous geocode must not trigger a place query')
    provider = Ambiguous()
    controller = TravelSkillOrchestrator(registry(), provider)
    assert await controller.candidate_context([{'city': 'Springfield'}],
                  request='Plan a trip to Springfield', fields={}) == {}
    assert [row[0] for row in provider.calls] == ['geocode']
    assert any(t['status'] == 'ambiguous_or_missing' for t in controller.trace)


@pytest.mark.asyncio
async def test_provider_failure_does_not_abort_daily_generation():
    class OfflineFailure(MockProviders):
        async def request(self, capability, **kwargs):
            raise ProviderUnavailable('Service unavailable')
    controller = TravelSkillOrchestrator(registry(), OfflineFailure())
    assert await controller.candidate_context([{'city':'Osaka'}],
                  request='Eat in Osaka', fields={'destination':'Japan'}) == {}
    assert any(t['status'] == 'error' for t in controller.trace)


@pytest.mark.asyncio
async def test_multiple_cities_each_receive_distinct_candidates():
    class MultiCity(MockProviders):
        async def request(self, capability, **kwargs):
            self.calls.append((capability, kwargs))
            if capability == 'geocode':
                city = kwargs['place']
                return ProviderResult({'results': [{'name': city, 'country':'Japan',
                                                   'latitude': 34.5 if city == 'Osaka' else 35.0,
                                                   'longitude': 135.5}]}, [])
            city = 'Osaka' if kwargs['latitude'] == 34.5 else 'Kyoto'
            return ProviderResult({'results': [{'name': f'{city} Museum',
                                                 'latitude': kwargs['latitude'],
                                                 'longitude': kwargs['longitude']}]}, [])
    provider = MultiCity()
    controller = TravelSkillOrchestrator(registry(), provider)
    results = await controller.candidate_context([{'city':'Osaka','theme':'heritage'},
                                                    {'city':'Kyoto','theme':'heritage'},
                                                    {'city':'Osaka','theme':'heritage'}],
                    request='Explore historical places', fields={'destination':'Japan'})
    assert set(results) == {'osaka', 'kyoto'}
    assert results['osaka']['places'][0]['name'] == 'Osaka Museum'
    assert results['kyoto']['places'][0]['name'] == 'Kyoto Museum'
    assert sum(c == 'geocode' for c, _ in provider.calls) == 2
