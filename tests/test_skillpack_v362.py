"""Compatibility acceptance tests for the active nine-Skill itinerary scope.

The original V3.6.2 suite covered eighteen Skills, nine of which were
intentionally retired by the itinerary-first product migration. Its archived
source is retained under tests/legacy for historical comparison.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from aligo.contracts import Evidence
from aligo.itinerary_skill_scope import ACTIVE_ITINERARY_SKILLS, RETIRED_UTILITY_SKILLS
from aligo.providers import ProviderResult, ProviderUnavailable
from aligo.registry import SkillRegistry

ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / 'plugins' / 'skills'


class Providers:
    def __init__(self):
        self.calls = []

    async def request(self, name, **kwargs):
        self.calls.append((name, kwargs))
        if name == 'geocode':
            return ProviderResult({'results': [{
                'name': kwargs['place'], 'country': 'Japan',
                'latitude': 34.7, 'longitude': 135.5,
            }]}, [Evidence(provider='mock-geocoder')])
        if name == 'places':
            venue = {'restaurant': {'name': 'Mapped Dining Venue', 'cuisine': 'Japanese'},
                     'tourism': {'name': 'Mapped Museum'}}.get(name if name != 'places'
                                                              else kwargs['kind'], {'name': 'Mapped Viewpoint'})
            return ProviderResult({'results': [{**venue, 'latitude': 34.7,
                                                'longitude': 135.5, 'osm_id': 123}]},
                                  [Evidence(provider='mock-map')])
        if name == 'weather':
            return ProviderResult({'timezone': 'Asia/Tokyo', 'hourly': {
                'time': ['2026-10-20T08:00'], 'temperature_2m': [16],
                'precipitation_probability': [30],
            }}, [Evidence(provider='mock-weather')])
        if name == 'place_details':
            return ProviderResult({'results': [{
                'place_id': kwargs['place_id'], 'name': 'Example Museum',
                'opening_hours': 'Mo-Fr 10:00-17:00',
            }]}, [Evidence(provider='fixture-place-details')])
        raise ProviderUnavailable('Offline fixture has no other capabilities.')


@pytest.fixture
def ctx():
    return SimpleNamespace(providers=Providers(), store=None, user_id='user-a',
                           session_id='sample', fields={'destination': 'Osaka'},
                           shared={}, query='Explore Japan')


def test_catalog_matches_nine_skill_product_scope():
    registry = SkillRegistry(SKILLS)
    assert set(registry.specs) == ACTIVE_ITINERARY_SKILLS
    assert len(registry.specs) == 18
    assert not set(registry.specs).intersection(RETIRED_UTILITY_SKILLS)
    for name in RETIRED_UTILITY_SKILLS:
        assert registry.get(name) is None


@pytest.mark.parametrize('name', sorted(ACTIVE_ITINERARY_SKILLS))
@pytest.mark.asyncio
async def test_each_active_skill_executes_documented_example(name, ctx):
    folder = SKILLS / name
    spec = SkillRegistry(SKILLS).get(name)
    assert spec is not None and spec.entrypoint == 'handler.py:run'
    instructions = (folder / 'SKILL.md').read_text(encoding='utf-8')
    assert '## Execution protocol' in instructions and len(instructions.splitlines()) >= 40
    args = json.loads((folder / 'references' / 'input_example.json').read_text(encoding='utf-8'))
    result = await SkillRegistry(SKILLS).run(name, ctx, args)
    assert result.status in {'ok', 'partial'}, (name, result)
    assert isinstance(result.data, dict) and result.data, (name, result)
    assert result.text and isinstance(result.limitations, list)


@pytest.mark.asyncio
async def test_place_and_restaurant_candidates_are_provider_grounded(ctx):
    registry = SkillRegistry(SKILLS)
    place = await registry.run('place-discovery', ctx, {'destination': 'Osaka', 'kind': 'tourism'})
    assert place.data['results'][0]['name'] == 'Mapped Museum'
    assert any(entry.provider == 'mock-map' for entry in place.sources)
    restaurant = await registry.run('restaurant-discovery', ctx,
                                    {'city': 'Osaka', 'cuisine': 'Japanese'})
    assert restaurant.data['results'][0]['name'] == 'Mapped Dining Venue'
    assert any(entry.provider == 'mock-map' for entry in restaurant.sources)


@pytest.mark.asyncio
async def test_calendar_conflicts_cover_transfer_requirements(ctx):
    events = [
        {'name': 'Museum', 'start': '2026-10-20T10:00', 'end': '2026-10-20T12:00',
         'transfer_buffer_minutes': 30},
        {'name': 'Lunch', 'start': '2026-10-20T12:15', 'end': '2026-10-20T13:30'},
    ]
    checked = await SkillRegistry(SKILLS).run('schedule-conflict-validation', ctx,
                                               {'events': events})
    assert checked.data['conflicts']
    assert checked.data['conflicts'][0]['required_buffer_minutes'] == 30
    assert checked.data['conflicts'][0]['reason'] == 'insufficient_transfer_buffer'


@pytest.mark.asyncio
async def test_unknown_place_and_unavailable_weather_remain_unverified(ctx):
    registry = SkillRegistry(SKILLS)
    unknown = await registry.run('place-detail-verification', ctx,
                                 {'place': 'Unknown Place', 'candidates': []})
    assert unknown.data['verification_level'] == 'unverified'
    ctx.providers = None
    weather = await registry.run('hourly-weather-forecast', ctx, {'city': 'Osaka'})
    assert weather.status == 'unavailable' and not weather.sources


@pytest.mark.asyncio
async def test_itinerary_replanning_preserves_the_original_activities(ctx):
    day = [{'place': 'Museum', 'start_time': '10:00'},
           {'place': 'Park', 'start_time': '13:00'}]
    response = await SkillRegistry(SKILLS).run('disruption-replanning', ctx, {
        'activities': day, 'disruption': {'type': 'closure', 'affected_places': ['Museum']},
    })
    assert response.data['affected_activity_indices'] == [0]
    assert response.data['requires_approval'] is True
    assert 'replanning_action' not in day[0]


@pytest.mark.asyncio
async def test_cultural_experience_does_not_invent_bookability(ctx):
    result = await SkillRegistry(SKILLS).run('cultural-experience-search', ctx, {
        'interest': 'ceramics', 'candidates': [
            {'name': 'Ceramics Gallery', 'category': 'ceramics',
             'booking_url': 'https://example.org/gallery'},
            {'name': 'Ceramics Workshop', 'category': 'ceramics',
             'organizer': 'Local Studio', 'booking_url': 'https://example.org/booking',
             'session_id': 'session-1'},
        ],
    })
    assert len(result.data['results']) == 2
    assert all(item['is_bookable_workshop'] is False for item in result.data['results'])
    assert all(item['booking_status'] == 'not_checked' for item in result.data['results'])


@pytest.mark.asyncio
async def test_route_optimization_preserves_fixed_endpoints_and_limitations(ctx):
    waypoints = [
        {'name': 'Start', 'latitude': 35.0, 'longitude': 135.0},
        {'name': 'Far Stop', 'latitude': 35.3, 'longitude': 135.3},
        {'name': 'Near Stop', 'latitude': 35.01, 'longitude': 135.01},
        {'name': 'Finish', 'latitude': 35.4, 'longitude': 135.4},
    ]
    response = await SkillRegistry(SKILLS).run('multi-stop-route-optimizer', ctx,
                                               {'waypoints': waypoints, 'fixed_destination': True})
    assert response.data['ordered_indices'][0] == 0
    assert response.data['ordered_indices'][-1] == len(waypoints) - 1
    assert response.data['total_km_straight_line'] <= response.data['original_km_straight_line']
    assert any('Straight-line' in item for item in response.limitations)


@pytest.mark.asyncio
async def test_schedule_timezone_conflicts_are_handled_without_exception(ctx):
    result = await SkillRegistry(SKILLS).run('schedule-conflict-validation', ctx, {
        'events': [
            {'start': '2026-10-20T09:00', 'end': '2026-10-20T10:00'},
            {'start': '2026-10-20T11:00+09:00', 'end': '2026-10-20T12:00+09:00'},
        ],
    })
    assert result.status == 'partial'
    assert 'mixed_timezones' in result.data['invalid_events']


@pytest.mark.asyncio
async def test_place_details_keep_provider_evidence_without_claiming_open_hours(ctx):
    result = await SkillRegistry(SKILLS).run('place-detail-verification', ctx, {
        'place': 'Example Museum', 'place_id': 'abcd1234567890', 'candidates': [],
    })
    assert result.data['provider_details'][0]['name'] == 'Example Museum'
    assert result.data['verification_level'] == 'provider_details_candidate'
    assert result.sources[0].provider == 'fixture-place-details'
