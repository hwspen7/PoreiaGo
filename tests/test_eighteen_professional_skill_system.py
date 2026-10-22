"""Functional and integration acceptance for the eighteen-Skill product catalog."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from aligo.api import create_app
from aligo.itinerary_skill_scope import ACTIVE_ITINERARY_SKILLS
from aligo.registry import SkillRegistry
from aligo.travel_intelligence import TravelSkillOrchestrator

ROOT = Path(__file__).resolve().parents[1]
NEW = {
    'neighborhood-intelligence', 'opening-hours-intelligence',
    'ticket-entry-planning', 'local-events-discovery',
    'culinary-specialty-discovery', 'seasonal-experience-intelligence',
    'accessibility-suitability', 'day-trip-discovery',
    'itinerary-feasibility-audit',
}


def registry():
    return SkillRegistry(ROOT / 'plugins' / 'skills')


async def execute(skill, args):
    return await registry().run(skill, SimpleNamespace(providers=None, fields={}), args)


def test_exactly_eighteen_independent_skill_entries():
    assert len(ACTIVE_ITINERARY_SKILLS) == 18
    assert set(registry().specs) == ACTIVE_ITINERARY_SKILLS
    for name in NEW:
        path = ROOT / 'plugins' / 'skills' / name
        assert (path / 'handler.py').exists()
        assert (path / 'references/input_example.json').exists()
        assert (path / 'tests').is_dir()


@pytest.mark.parametrize('skill', sorted(NEW))
@pytest.mark.asyncio
async def test_each_advanced_skill_executes_without_network(skill):
    path = ROOT / 'plugins' / 'skills' / skill
    args = json.loads((path / 'references/input_example.json').read_text())
    result = await execute(skill, args)
    assert result.status == 'partial'
    assert isinstance(result.data, dict) and result.data
    assert result.limitations
    assert not result.sources


@pytest.mark.asyncio
async def test_neighborhood_ranking_is_evidence_bound():
    result = await execute('neighborhood-intelligence', {'candidates': [
        {'name': 'Gallery', 'area': 'Arts Quarter', 'category': 'art'},
        {'name': 'Craft Studio', 'area': 'Arts Quarter', 'category': 'art'},
        {'name': 'Garden', 'area': 'Park', 'category': 'nature'}],
        'interests': ['art']})
    assert result.data['results'][0]['name'] == 'Arts Quarter'
    assert result.data['results'][0]['venue_count'] == 2
    assert 'safety' in ' '.join(result.limitations).lower()


@pytest.mark.asyncio
async def test_opening_hours_unknowns_and_supplied_weekday():
    result = await execute('opening-hours-intelligence', {'visit_date': '2026-10-20',
        'candidates': [{'name': 'A', 'weekly_hours': {'tue': [{'open': '10:00', 'close': '17:00'}]}},
                       {'name': 'B'}, {'name': 'C', 'weekly_hours': {'tue': []}}]})
    status = {r['name']: r['hours_status'] for r in result.data['results']}
    assert status == {'A': 'scheduled_hours_supplied', 'B': 'unknown',
                      'C': 'known_closed_in_supplied_schedule'}


@pytest.mark.asyncio
async def test_ticket_records_do_not_claim_booking_or_live_prices():
    result = await execute('ticket-entry-planning', {'candidates': [
        {'name': 'Museum', 'reservation_required': True, 'ticket_price': 19,
         'currency': 'USD', 'booking_url': 'https://example.test/entry'}]})
    record = result.data['results'][0]
    assert record['reservation_required_claim'] is True
    assert record['listed_price'] == 19
    assert record['booking_status'] == 'not_checked'
    assert record['entry_status'] == 'not_checked'


@pytest.mark.asyncio
async def test_events_dated_filter_and_unknown_omission():
    result = await execute('local-events-discovery', {'start_date': '2026-10-21',
       'end_date': '2026-10-23', 'events': [
           {'name': 'Event A', 'start_date': '2026-10-22'},
           {'name': 'Event B', 'start_date': '2026-11-01'},
           {'name': 'Undated event'}]})
    assert [r['name'] for r in result.data['results']] == ['Event A']
    assert result.data['undated_omitted'] == 1


@pytest.mark.asyncio
async def test_culinary_filters_supplied_dietary_avoidance_without_safety_claims():
    result = await execute('culinary-specialty-discovery', {'avoid_ingredients': ['peanut'],
       'candidates': [{'name': 'Unsafe', 'ingredients': 'peanut butter'},
                      {'name': 'Unknown', 'cuisine': 'regional'}]})
    assert [r['name'] for r in result.data['results']] == ['Unknown']
    assert result.data['results'][0]['dietary_status'] == 'not_verified'


@pytest.mark.asyncio
async def test_seasonality_requires_supplied_months():
    result = await execute('seasonal-experience-intelligence', {'month': 10,
       'candidates': [{'name': 'Autumn Garden', 'season_months': [9, 10]},
                      {'name': 'Spring Trail', 'season_months': [4, 5]},
                      {'name': 'Unknown Bloom'}]})
    assert [r['name'] for r in result.data['results']] == ['Autumn Garden']
    assert result.data['missing_season_metadata'] == 1


@pytest.mark.asyncio
async def test_accessibility_missing_data_is_not_passed_as_accessible():
    result = await execute('accessibility-suitability', {'requirements': ['step_free'],
       'candidates': [{'name': 'Venue A'}, {'name': 'Venue B', 'accessibility': {'step_free': False}}]})
    a, b = result.data['results']
    assert a['requirements']['step_free'] is None
    assert b['requirements']['step_free'] is False
    assert b['clearly_marked_unavailable'] == ['step_free']


@pytest.mark.asyncio
async def test_day_trip_distance_has_no_fabricated_travel_time():
    result = await execute('day-trip-discovery', {'origin': 'Base City',
       'candidates': [{'name': 'Near Town', 'distance_km': 80},
                      {'name': 'Far Town', 'distance_km': 380}], 'max_distance_km': 140})
    assert [r['name'] for r in result.data['results']] == ['Near Town']
    assert result.data['results'][0]['route_time_status'] == 'not_verified'


@pytest.mark.asyncio
async def test_feasibility_audit_is_read_only_and_reports_overlap():
    activities = [
        {'place_name': 'Museum', 'start_time': '10:00', 'duration_minutes': 120},
        {'place_name': 'Gallery', 'start_time': '11:00', 'duration_minutes': 60},
        {'place_name': 'Museum', 'start_time': '14:00', 'duration_minutes': 60},
    ]
    result = await execute('itinerary-feasibility-audit', {'activities': activities})
    assert result.data['auto_modified'] is False
    assert {'time_overlap', 'repeated_place'} <= {r['type'] for r in result.data['risks']}
    assert activities[1]['start_time'] == '11:00'


@pytest.mark.asyncio
async def test_orchestrator_selects_relevant_specialties_instead_of_all_nine():
    orchestrator = TravelSkillOrchestrator(registry(), None)
    candidates = [
        {'name': 'Old Museum', 'area': 'Downtown', 'weekly_hours': {
            'tue': [{'open': '10:00', 'close': '17:00'}]}, 'reservation_required': True},
        {'name': 'District Cafe', 'area': 'Downtown', 'type': 'restaurant', 'cuisine': 'regional'},
    ]
    extras = await orchestrator.professional_day_context(candidates,
        {'city': 'Sample City', 'day': 1, 'theme': 'Local food', 'date': '2026-10-20'},
        request='Explore museum and local food', fields={})
    assert {'neighborhood-intelligence', 'opening-hours-intelligence',
            'ticket-entry-planning', 'culinary-specialty-discovery'} <= set(extras)
    assert 'day-trip-discovery' not in extras
    assert 'seasonal-experience-intelligence' not in extras
    assert len(extras) <= 5


@pytest.mark.asyncio
async def test_offline_user_candidates_reach_daily_model_context():
    orchestrator = TravelSkillOrchestrator(registry(), None)
    context = await orchestrator.day_context({},
        {'city': 'Sample City', 'day': 1, 'theme': 'Art'},
        request='Explore museums', fields={'venue_candidates': [
            {'name': 'Example Museum', 'neighborhood': 'Old Quarter'}]})
    assert 'neighborhood-intelligence' in context['professional_evidence']


@pytest.mark.asyncio
async def test_final_audit_traced_not_forced_into_generated_itinerary():
    orchestrator = TravelSkillOrchestrator(registry(), None)
    issues = await orchestrator.schedule_issues({'activities': [
        {'place_name': 'Museum', 'time': '10:00', 'duration_minutes': 90},
        {'place_name': 'Gallery', 'time': '12:00', 'duration_minutes': 60},
    ]})
    assert issues == []
    assert any(row['skill'] == 'itinerary-feasibility-audit' for row in orchestrator.trace)


@pytest.mark.asyncio
async def test_public_skill_api_shows_all_eighteen(tmp_path):
    app = create_app(database_path=tmp_path / 'eighteen.sqlite3')
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://testclient') as client:
        response = await client.get('/api/skills')
        assert response.status_code == 200
        assert response.json()['count'] == 18
        assert {row['id'] for row in response.json()['skills']} == ACTIVE_ITINERARY_SKILLS


@pytest.mark.asyncio
async def test_event_provider_is_optional_and_provenance_is_retained():
    from aligo.contracts import Evidence
    from aligo.providers import ProviderResult

    class EventsProvider:
        def available(self, capability):
            return capability == 'events'

        async def request(self, capability, **kwargs):
            assert capability == 'events'
            assert kwargs['city'] == 'Example City'
            return ProviderResult({'results': [{
                'name': 'Autumn Concert', 'start_date': '2026-10-22',
                'event_id': 'concert-1'}]}, [Evidence(provider='test-event-source')])

    response = await registry().run('local-events-discovery',
        SimpleNamespace(fields={}, providers=EventsProvider()),
        {'city': 'Example City', 'start_date': '2026-10-21', 'end_date': '2026-10-23'})
    assert response.data['results'][0]['name'] == 'Autumn Concert'
    assert response.sources[0].provider == 'test-event-source'
