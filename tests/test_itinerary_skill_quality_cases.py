"""Behavioral regression gates for the nine itinerary-first specialist skills."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from aligo.registry import SkillRegistry
from aligo.contracts import Evidence
from aligo.providers import ProviderResult


ROOT = Path(__file__).resolve().parents[1]


class Provider:
    def __init__(self, *, places=None, weather=None, geocode=None, details=None):
        self.places = places or []
        self.weather = weather or {'hourly': {'time': [], 'precipitation_probability': []}}
        self.geocode = geocode or [{'name': 'Osaka', 'country': 'Japan',
                                     'latitude': 34.69, 'longitude': 135.50}]
        self.details = details or []
        self.calls = []

    async def request(self, name, **kwargs):
        self.calls.append((name, kwargs))
        data = {'places': {'results': self.places},
                'geocode': {'results': self.geocode},
                'weather': self.weather,
                'place_details': {'results': self.details}}[name]
        return ProviderResult(data, [Evidence(provider='test-provider')])


@pytest.fixture
def registry():
    return SkillRegistry(ROOT / 'plugins' / 'skills')


def ctx(providers=None):
    return SimpleNamespace(providers=providers, fields={'destination': 'Osaka'},
                           query='Please design a day', store=None, user_id='test',
                           session_id='test', shared={})


@pytest.mark.asyncio
async def test_place_candidates_rank_by_interest_and_exclude_previous_days(registry):
    candidates = [
        {'name': 'Repeated Garden', 'category': 'garden', 'osm_id': 1},
        {'name': 'Workshop Museum', 'category': 'craft museum', 'osm_id': 2, 'address': 'Osaka'},
        {'name': 'Ordinary Square', 'category': 'park', 'osm_id': 3},
        {'name': 'Workshop Museum', 'category': 'craft museum', 'osm_id': 2},
    ]
    res = await registry.run('place-discovery', ctx(),
                             {'candidates': candidates, 'interests': ['craft'],
                              'visited_names': ['Repeated Garden']})
    assert [r['name'] for r in res.data['results']] == ['Workshop Museum', 'Ordinary Square']
    assert res.data['candidate_diagnostics']['duplicate'] == 1
    assert res.data['candidate_diagnostics']['excluded'] == 1
    assert res.data['results'][0]['visit_status'] == 'not_confirmed'


@pytest.mark.asyncio
async def test_discovery_requires_city_disambiguation(registry):
    provider = Provider(geocode=[{'name': 'Springfield', 'country': 'United States',
                                  'latitude': 39.0, 'longitude': -83.0},
                                 {'name': 'Springfield', 'country': 'Australia',
                                  'latitude': -27.6, 'longitude': 152.9}])
    res = await registry.run('place-discovery', ctx(provider), {'city': 'Springfield'})
    assert res.status == 'partial'
    assert not any(n == 'places' for n, _ in provider.calls)


@pytest.mark.asyncio
async def test_place_verification_prevents_id_substitution(registry):
    res = await registry.run('place-detail-verification', ctx(),
                             {'name': 'Castle', 'place_id': 'place-123',
                              'candidates': [{'name': 'Castle', 'place_id': 'place-456'}]})
    assert res.data['match'] is None
    assert res.data['verification_level'] == 'unverified'


@pytest.mark.asyncio
async def test_place_verification_respects_city_and_source_details(registry):
    provider = Provider(details=[{'name': 'Castle', 'place_id': 'actual',
                                  'city': 'Osaka', 'country': 'Japan', 'osm_id': 9},
                                 {'name': 'Different', 'place_id': 'other', 'city': 'Osaka'}])
    res = await registry.run('place-detail-verification', ctx(provider),
                             {'place_id': 'actual', 'city': 'Osaka', 'country': 'Japan'})
    assert res.data['match']['name'] == 'Castle'
    assert len(res.data['provider_details']) == 1
    assert res.data['verification_level'] == 'provider_details_candidate'


@pytest.mark.asyncio
async def test_restaurants_rank_by_area_and_exclude_repeats(registry):
    candidates = [{'name': 'First', 'cuisine': 'soba', 'area': 'Gion', 'osm_id': 1},
                  {'name': 'Second', 'cuisine': 'soba', 'area': 'Kyoto Station', 'osm_id': 2},
                  {'name': 'First', 'cuisine': 'soba', 'osm_id': 1},
                  {'name': 'Closed Choice', 'cuisine': 'ramen', 'osm_id': 3}]
    res = await registry.run('restaurant-discovery', ctx(),
                             {'candidates': candidates, 'area': 'Gion', 'cuisine': 'soba',
                              'already_planned': ['Second']})
    assert [r['name'] for r in res.data['results']] == ['First']
    assert res.data['results'][0]['booking_status'] == 'not_checked'
    assert res.data['results'][0]['price_status'] == 'not_checked'


@pytest.mark.asyncio
async def test_restaurants_do_not_assert_dietary_safety(registry):
    res = await registry.run('restaurant-discovery', ctx(),
                             {'candidates': [{'name': 'Sample', 'cuisine': 'Japanese'}],
                              'dietary_requirement': 'gluten free'})
    assert res.data['results'][0]['dietary_status'] == 'not_checked'


@pytest.mark.asyncio
async def test_culture_requires_interest_and_cannot_claim_booking(registry):
    res = await registry.run('cultural-experience-search', ctx(),
                             {'candidates': [{'name': 'Tea Workshop', 'category': 'tea',
                                              'organizer': 'Local Host', 'booking_url': 'https://example.org',
                                              'session_id': 'event-1'},
                                             {'name': 'Unrelated Stadium', 'category': 'sport'}],
                              'interest': 'tea'})
    assert len(res.data['results']) == 1
    row = res.data['results'][0]
    assert row['specific_session_identified'] is True
    assert row['booking_reference_present'] is True
    assert row['is_bookable_workshop'] is False


@pytest.mark.asyncio
async def test_culture_does_not_invent_sessions(registry):
    res = await registry.run('cultural-experience-search', ctx(),
                             {'candidates': [{'name': 'Craft Museum', 'category': 'craft'}],
                              'interest': 'craft'})
    assert res.data['specific_session_candidates'] == 0
    assert res.data['results'][0]['booking_status'] == 'not_checked'


@pytest.mark.asyncio
async def test_optimizer_preserves_endpoints_and_reports_long_legs(registry):
    points = [{'name': 'Start', 'area': 'West', 'latitude': 35.0, 'longitude': 135.0},
              {'name': 'Far', 'area': 'East', 'latitude': 36.0, 'longitude': 136.0},
              {'name': 'Near', 'area': 'West', 'latitude': 35.01, 'longitude': 135.01},
              {'name': 'Finish', 'area': 'East', 'latitude': 35.02, 'longitude': 135.02}]
    res = await registry.run('multi-stop-route-optimizer', ctx(),
                             {'waypoints': points, 'fixed_destination': True, 'max_leg_km': 5})
    assert res.data['ordered_indices'][0] == 0
    assert res.data['ordered_indices'][-1] == 3
    assert res.data['overlong_leg_indices']
    assert 'straight-line' in ' '.join(res.limitations).lower() or 'walking' in ' '.join(res.limitations)


@pytest.mark.asyncio
async def test_optimizer_rejects_invalid_coordinates_and_thresholds(registry):
    points = [{'latitude': 35, 'longitude': 135}, {'latitude': 35.1, 'longitude': 135.1}]
    with pytest.raises(ValueError, match='Invalid arguments'):
        await registry.run('multi-stop-route-optimizer', ctx(),
                           {'waypoints': points, 'max_total_km': -1})


@pytest.mark.asyncio
async def test_schedule_detects_nested_conflicts(registry):
    events = [{'name': 'Long Tour', 'start': '2026-10-20T09:00', 'end': '2026-10-20T13:00'},
              {'name': 'Coffee', 'start': '2026-10-20T10:00', 'end': '2026-10-20T11:00'},
              {'name': 'Lunch', 'start': '2026-10-20T11:30', 'end': '2026-10-20T12:00'}]
    res = await registry.run('schedule-conflict-validation', ctx(), {'events': events})
    assert sum(c['reason'] == 'overlap' for c in res.data['conflicts']) == 2


@pytest.mark.asyncio
async def test_schedule_combines_incoming_transfer_and_buffer(registry):
    events = [{'name': 'Museum', 'start': '2026-10-20T09:00', 'end': '2026-10-20T11:00'},
              {'name': 'Lunch', 'start': '2026-10-20T11:30', 'end': '2026-10-20T12:30',
               'transfer_minutes': 25, 'buffer_minutes': 10}]
    res = await registry.run('schedule-conflict-validation', ctx(), {'events': events})
    assert res.data['conflicts'][0]['required_buffer_minutes'] == 35
    assert res.data['conflicts'][0]['shortfall_minutes'] == 5
    events[1]['transfer_minutes'] = 20
    res = await registry.run('schedule-conflict-validation', ctx(), {'events': events})
    assert res.data['conflicts'] == []


@pytest.mark.asyncio
async def test_weather_date_specific_risk_and_no_phantom_hours(registry):
    provider = Provider(weather={'hourly': {'time': ['2026-11-01T10:00', '2026-11-01T11:00',
                                                    '2026-11-02T10:00'],
                                           'temperature_2m': [15, 16, 18],
                                           'precipitation_probability': [70, 80, 95]}})
    res = await registry.run('hourly-weather-forecast', ctx(provider),
                             {'latitude': 34.69, 'longitude': 135.50, 'date': '2026-11-01'})
    assert res.data['forecast_hours_checked'] == 2
    assert len(res.data['grouped_rain_windows']) == 1
    assert res.data['grouped_rain_windows'][0]['peak_probability_pct'] == 80
    assert res.data['outdoor_plan_review_needed'] is True


@pytest.mark.asyncio
async def test_weather_missing_date_marks_partial(registry):
    provider = Provider(weather={'hourly': {'time': ['2026-11-01T10:00'],
                                           'precipitation_probability': [20]}})
    res = await registry.run('hourly-weather-forecast', ctx(provider),
                             {'latitude': 35, 'longitude': 135, 'date': '2026-12-01'})
    assert res.status == 'partial'
    assert res.data['forecast_hours_checked'] == 0


@pytest.mark.asyncio
async def test_disruption_suggests_only_unused_replacements(registry):
    visits = [{'place_name': 'Closed Museum', 'start_time': '09:00'},
              {'place_name': 'Lunch', 'start_time': '12:00'}]
    before = [dict(row) for row in visits]
    res = await registry.run('disruption-replanning', ctx(),
                             {'activities': visits,
                              'disruption': {'type': 'closure', 'affected_places': ['Closed Museum']},
                              'alternative_candidates': [{'name': 'Craft Gallery'}, {'name': 'Lunch'}],
                              'visited_names': ['Old Museum']})
    assert visits == before
    assert res.data['proposal'][0]['replacement_candidates'][0]['name'] == 'Craft Gallery'
    assert res.data['proposal'][1] == visits[1]
    assert res.data['requires_approval'] is True


@pytest.mark.asyncio
async def test_disruption_does_not_move_fixed_bookings(registry):
    visits = [{'start_time': '10:00'}, {'start_time': '12:00', 'booking_confirmed': True},
              {'start_time': '15:00'}]
    res = await registry.run('disruption-replanning', ctx(),
                             {'activities': visits, 'disruption': {
                                 'type': 'delay', 'delay_minutes': 30, 'start_activity_index': 0}})
    assert res.data['fixed_time_conflict_indices'] == [1]
    assert res.data['proposal'][1]['start_time'] == '12:00'
    assert 'suggested_start_time' not in res.data['proposal'][1]


@pytest.mark.asyncio
async def test_budget_basis_and_daily_envelopes(registry):
    res = await registry.run('trip-budget-allocation', ctx(),
                             {'total': 1000, 'travelers': 2, 'days': 3,
                              'basis': 'per_person', 'day_weights': [1, 2, 1]})
    assert res.status == 'ok'
    assert res.data['group_total'] == 2000
    assert res.data['per_person_total'] == 1000
    assert round(sum(res.data['daily_on_site_envelopes']), 2) == 320
    assert res.data['daily_on_site_envelopes'][1] == 160


@pytest.mark.asyncio
async def test_budget_invalid_weights_dont_create_false_figures(registry):
    res = await registry.run('trip-budget-allocation', ctx(),
                             {'total': 1000, 'travelers': 2, 'days': 2,
                              'day_weights': [0, 0]})
    assert res.status == 'partial'
    assert 'allocation' not in res.data


@pytest.mark.asyncio
async def test_all_nine_skills_have_local_execution_and_documents(registry):
    expected = {'place-discovery', 'place-detail-verification', 'restaurant-discovery',
                'cultural-experience-search', 'multi-stop-route-optimizer',
                'schedule-conflict-validation', 'hourly-weather-forecast',
                'disruption-replanning', 'trip-budget-allocation'}
    root = ROOT / 'plugins' / 'skills'
    assert {p.name for p in root.iterdir() if p.is_dir() and (p / "manifest.json").exists()} >= expected
    for name in expected:
        assert (root / name / 'handler.py').is_file()
        assert (root / name / 'SKILL.md').is_file()
        assert (root / name / 'references' / 'OPERATIONS.md').is_file()
        assert registry.get(name).entrypoint == 'handler.py:run'

@pytest.mark.asyncio
async def test_orchestrator_passes_itinerary_preferences_into_actual_skill_selection(registry):
    from aligo.travel_intelligence import TravelSkillOrchestrator

    class IntegratedProvider(Provider):
        def available(self, name):
            return name in {'geocode', 'places'}
        async def request(self, name, **kwargs):
            if name == 'places':
                kind = kwargs.get('kind')
                if kind == 'restaurant':
                    self.places = [{'name': 'Tea House', 'category': 'restaurant',
                                    'cuisine': 'tea', 'osm_id': 30},
                                   {'name': 'Rejected Cafe', 'osm_id': 31}]
                else:
                    self.places = [{'name': 'Craft Museum', 'category': 'craft museum',
                                    'osm_id': 10, 'address': 'Osaka'},
                                   {'name': 'Already Seen', 'category': 'museum', 'osm_id': 11}]
            return await super().request(name, **kwargs)

    provider = IntegratedProvider()
    orchestration = TravelSkillOrchestrator(registry, provider)
    result = await orchestration.candidate_context(
        [{'city': 'Osaka', 'theme': 'food and artisan crafts'}],
        request='Please suggest food and craft activities',
        fields={'country': 'Japan', 'interests': ['craft'],
                'avoid_places': ['Already Seen', 'Rejected Cafe'], 'cuisine': 'tea'})
    names = [row['name'] for row in result['osaka']['places']]
    assert 'Craft Museum' in names and 'Tea House' in names
    assert 'Already Seen' not in names and 'Rejected Cafe' not in names
    assert any(row.get('matching_score', 0) > 0 for row in result['osaka']['places'])
    assert result['osaka']['experiences']
    assert any(row['skill'] == 'cultural-experience-search' for row in orchestration.trace)

@pytest.mark.asyncio
async def test_orchestrator_propagates_transfer_plus_buffer_as_distinct_failure(registry):
    from aligo.travel_intelligence import TravelSkillOrchestrator
    orchestrator = TravelSkillOrchestrator(registry, None)
    day = {'activities': [
        {'start_time': '09:00', 'duration_minutes': 120},
        {'start_time': '11:30', 'duration_minutes': 60,
         'transfer_minutes': 25, 'buffer_minutes': 10},
    ]}
    warnings = await orchestrator.schedule_issues(day)
    assert any('transfer/buffer' in warning for warning in warnings)
    assert not any('overlap' in warning for warning in warnings)
