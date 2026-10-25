"""Regression cases for professional travel-skill decisions and safe limits."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from core.contracts import Evidence
from services.providers import ProviderResult, ProviderUnavailable
from services.registry import SkillRegistry
from services.travel_intelligence import TravelSkillOrchestrator


ROOT = Path(__file__).resolve().parents[2]


def skill_registry():
    return SkillRegistry(ROOT / 'plugins/skills')


def context(providers=None, store=None):
    return SimpleNamespace(providers=providers, store=store, fields={},
                           query='Travel request', user_id='traveler',
                           session_id='journey', shared={})


async def invoke(skill, args, providers=None, store=None):
    return await skill_registry().run(skill, context(providers, store), args)


class MappedProvider:
    def __init__(self):
        self.calls = []

    def available(self, capability):
        return True

    async def request(self, capability, **kwargs):
        self.calls.append((capability, kwargs))
        if capability == 'geocode':
            return ProviderResult({'results': [{'name': 'Sample City', 'country': 'Exampleland',
                                                'latitude': 40.0, 'longitude': 20.0}]},
                                  [Evidence(provider='geocoder')])
        if capability == 'places':
            kind = kwargs['kind']
            if kind == 'restaurant':
                rows = [{'name': 'Cafe A', 'osm_id': 1, 'cuisine': 'Sushi',
                         'address': 'Old Town', 'latitude': 40.0, 'longitude': 20.0},
                        {'name': 'Cafe B', 'osm_id': 2, 'cuisine': 'Pasta',
                         'address': 'New Town', 'latitude': 40.1, 'longitude': 20.0},
                        {'name': 'Cafe A', 'osm_id': 1, 'cuisine': 'Sushi'}]
            elif kind == 'hotel':
                rows = [{'name': 'Hotel A', 'osm_id': 3, 'address': 'Old Town'},
                        {'name': 'Hotel B', 'osm_id': 4, 'address': 'New Town'}]
            else:
                rows = [{'name': 'Craft Gallery', 'osm_id': 5, 'category': 'craft',
                         'latitude': 40.0, 'longitude': 20.0},
                        {'name': 'Historic Tower', 'osm_id': 6, 'category': 'heritage',
                         'latitude': 40.1, 'longitude': 20.1}]
            return ProviderResult({'results': rows}, [Evidence(provider='map-provider')])
        if capability == 'weather':
            return ProviderResult({'hourly': {'time': ['2026-11-01T10:00', '2026-11-01T11:00'],
                                              'temperature_2m': [17],
                                              'precipitation_probability': [80, 40]}},
                                  [Evidence(provider='forecast')])
        if capability in ('transit_route', 'walking_route'):
            return ProviderResult({'duration_s': 900, 'distance_m': 1900},
                                  [Evidence(provider='routing')])
        raise ProviderUnavailable('Not supported')


@pytest.mark.asyncio
async def test_nested_schedule_checks_all_pairs():
    base = '2026-11-01T'
    result = await invoke('schedule-conflict-validation', {'events': [
        {'name': 'Long visit', 'start': base + '09:00', 'end': base + '13:00'},
        {'name': 'Coffee', 'start': base + '10:00', 'end': base + '11:00'},
        {'name': 'Lunch', 'start': base + '11:30', 'end': base + '12:00'}]})
    assert len(result.data['conflicts']) == 2
    assert {r['next'] for r in result.data['conflicts']} == {'Coffee', 'Lunch'}


@pytest.mark.asyncio
async def test_transfer_buffer_is_distinct_from_overlap():
    result = await invoke('schedule-conflict-validation', {'events': [
        {'name': 'Museum', 'start': '2026-11-01T09:00', 'end': '2026-11-01T10:00',
         'transfer_buffer_minutes': 25},
        {'name': 'Garden', 'start': '2026-11-01T10:15', 'end': '2026-11-01T11:00'}]})
    assert result.data['conflicts'][0]['reason'] == 'insufficient_transfer_buffer'
    assert result.data['conflicts'][0]['overlap_minutes'] == 0


@pytest.mark.asyncio
async def test_mixed_timezone_is_rejected():
    result = await invoke('schedule-conflict-validation', {'events': [
        {'start': '2026-11-01T10:00+09:00', 'end': '2026-11-01T11:00+09:00'},
        {'start': '2026-11-01T12:00', 'end': '2026-11-01T13:00'}]})
    assert result.status == 'partial'
    assert 'mixed_timezones' in result.data['invalid_events']


@pytest.mark.asyncio
async def test_route_optimization_keeps_fixed_destination():
    stops = [{'latitude': 40, 'longitude': 20}, {'latitude': 40, 'longitude': 24},
             {'latitude': 40, 'longitude': 21}, {'latitude': 40, 'longitude': 22}]
    result = await invoke('multi-stop-route-optimizer',
                          {'waypoints': stops, 'fixed_destination': True})
    assert result.data['ordered_indices'][0] == 0
    assert result.data['ordered_indices'][-1] == 3
    assert result.data['improvement_km'] >= 0
    assert result.data['total_km_straight_line'] <= result.data['original_km_straight_line']


@pytest.mark.asyncio
async def test_route_optimizer_rejects_conflicting_route_constraints():
    stops = [{'latitude': 40, 'longitude': 20}, {'latitude': 40, 'longitude': 21}]
    result = await invoke('multi-stop-route-optimizer',
                          {'waypoints': stops, 'return_to_start': True, 'fixed_destination': True})
    assert result.status == 'partial'


@pytest.mark.asyncio
async def test_restaurant_filters_cuisine_and_exclusions_without_claiming_diet_safety():
    result = await invoke('restaurant-discovery',
                          {'latitude': 40, 'longitude': 20, 'cuisine': 'Sushi',
                           'dietary_requirement': 'gluten free'}, MappedProvider())
    assert [r['name'] for r in result.data['results']] == ['Cafe A']
    assert result.data['results'][0]['dietary_status'] == 'not_checked'
    assert (await invoke('restaurant-discovery', {'latitude': 40, 'longitude': 20,
            'exclude_names': ['Cafe A']}, MappedProvider())).data['results'][0]['name'] == 'Cafe B'


@pytest.mark.asyncio
async def test_cultural_candidate_does_not_misrepresent_workshop_booking():
    result = await invoke('cultural-experience-search', {'interest': 'craft', 'candidates': [
        {'name': 'Craft Gallery', 'category': 'craft', 'osm_id': 1},
        {'name': 'Booked Workshop', 'category': 'craft', 'organizer': 'Studio',
         'booking_url': 'https://example.org/workshop'}]})
    assert result.data['results'][0]['name'] == 'Booked Workshop'
    assert result.data['results'][0]['booking_reference_present'] is True
    assert all(r['booking_status'] == 'not_checked' and not r['is_bookable_workshop']
               for r in result.data['results'])


@pytest.mark.skip(reason="Legacy utility Skill retired from itinerary-first catalog")
@pytest.mark.asyncio
async def test_airport_prioritizes_real_endpoint_over_transfers():
    result = await invoke('airport-transfer', {'origin': 'Airport', 'destination': 'Hotel District',
        'route_options': [{'mode': 'train', 'origin': 'Airport', 'destination': 'Other City',
                           'transfers': 0, 'duration_minutes': 30},
                          {'mode': 'bus', 'origin': 'Airport', 'destination': 'Hotel District',
                           'transfers': 1, 'duration_minutes': 50}]})
    assert result.data['preferred_candidate']['destination'] == 'Hotel District'
    assert result.data['route_options'][1]['requires_endpoint_confirmation']


@pytest.mark.skip(reason="Legacy utility Skill retired from itinerary-first catalog")
@pytest.mark.asyncio
async def test_transit_endpoint_comparison_is_not_timetable_verification():
    result = await invoke('public-transit-router', {'origin': 'Station A', 'destination': 'Station B',
        'route_options': [{'mode': 'metro', 'origin': 'Station A', 'destination': 'Station C',
                           'transfers': 0},
                          {'mode': 'train', 'origin': 'Station A', 'destination': 'Station B',
                           'transfers': 1}]})
    assert result.data['preferred_candidate']['destination'] == 'Station B'
    assert result.status == 'partial'


@pytest.mark.skip(reason="Legacy utility Skill retired from itinerary-first catalog")
@pytest.mark.asyncio
async def test_source_verification_marks_old_and_future_records_unverified():
    result = await invoke('source-verification', {'sources': [
        {'provider': 'official', 'url': 'https://example.org', 'observed_at': '2020-01-01'},
        {'provider': 'official', 'url': 'https://example.org', 'observed_at': '2100-01-01'}]})
    assert result.data['stale_records'] == 2
    assert all(not row['claim_verified'] for row in result.data['records'])


@pytest.mark.skip(reason="Legacy utility Skill retired from itinerary-first catalog")
@pytest.mark.asyncio
async def test_cost_bands_kept_separate_across_currency_and_basis():
    result = await invoke('travel-cost-estimation', {'items': [
        {'amount': 10, 'maximum': 20, 'currency': 'USD', 'basis': 'per_person'},
        {'amount': 5, 'currency': 'USD', 'basis': 'per_person'},
        {'amount': 30, 'currency': 'USD', 'basis': 'group_total'},
        {'amount': 400, 'currency': 'JPY', 'basis': 'per_person'},
        {'amount': -2, 'currency': 'USD'}]})
    assert len(result.data['totals']) == 3
    assert result.data['invalid_indices'] == [4]
    band = next(row for row in result.data['totals'] if row['currency'] == 'USD'
                and row['basis'] == 'per_person')
    assert band['minimum'] == 15 and band['maximum'] == 25


@pytest.mark.asyncio
async def test_budget_maintains_per_person_and_group_basis():
    result = await invoke('trip-budget-allocation',
                          {'total': 1000, 'days': 5, 'travelers': 2, 'basis': 'per_person'})
    assert result.data['per_person_total'] == 1000
    assert result.data['group_total'] == 2000
    assert round(sum(result.data['allocation'].values()), 2) == 1000
    invalid = await invoke('trip-budget-allocation',
                           {'total': 1000, 'weights': {'food': 10, 'lodging': 10}})
    assert invalid.status == 'partial'


@pytest.mark.asyncio
async def test_weather_handles_missing_array_entries_and_rain_risk():
    result = await invoke('hourly-weather-forecast',
                          {'latitude': 40, 'longitude': 20, 'forecast_days': 1}, MappedProvider())
    assert result.data['forecast_hours_checked'] == 2
    assert len(result.data['rain_risk_windows']) == 1
    assert result.data['preview'][1]['temperature_c'] is None


class ExpenseStore:
    def __init__(self):
        self.rows = []

    def add_expense(self, user, session, amount, currency, category):
        self.rows.append({'user': user, 'session': session, 'amount': amount,
                          'currency': currency, 'category': category})

    def expenses(self, user):
        return [x for x in self.rows if x['user'] == user]


@pytest.mark.skip(reason="Legacy utility Skill retired from itinerary-first catalog")
@pytest.mark.asyncio
async def test_expenses_require_consent_and_separate_categories():
    store = ExpenseStore()
    declined = await invoke('expense-tracking', {'action': 'add', 'amount': 100}, store=store)
    assert declined.status == 'partial' and store.rows == []
    await invoke('expense-tracking', {'action': 'add', 'amount': 100,
                                     'category': 'food', 'consent': True}, store=store)
    await invoke('expense-tracking', {'action': 'add', 'amount': 40,
                                     'category': 'transport', 'consent': True}, store=store)
    report = await invoke('expense-tracking', {'action': 'list'}, store=store)
    assert report.data['totals_by_currency']['USD'] == 140
    assert len(report.data['totals_by_category']) == 2


@pytest.mark.asyncio
async def test_disruption_preserves_unaffected_prefix():
    activities = [{'description': 'Museum', 'start_time': '09:00'},
                  {'description': 'Lunch', 'start_time': '12:00'},
                  {'description': 'Gallery', 'start_time': '15:00'}]
    result = await invoke('disruption-replanning', {'activities': activities,
         'disruption': {'type': 'delay', 'start_activity_index': 1, 'delay_minutes': 90}})
    assert 'suggested_start_time' not in result.data['proposal'][0]
    assert result.data['proposal'][1]['suggested_start_time'] == '13:30'
    assert activities[1]['start_time'] == '12:00'


@pytest.mark.skip(reason="Legacy utility Skill retired from itinerary-first catalog")
@pytest.mark.asyncio
async def test_export_keeps_venue_and_budget_info():
    result = await invoke('itinerary-export', {'format': 'markdown', 'itinerary': {
        'title': 'Weekend', 'days': [{'day': 1, 'title': 'Daytrip',
        'overnight_base': 'Hotel district', 'activities': [
          {'description': 'Museum', 'start_time': '09:00', 'cost_estimate': '20 USD',
           'transport_hint': 'Walk from hotel'}], 'budget_note': '50 USD per person'}],
        'predeparture_checks': ['Confirm museum hours']}})
    assert 'Confirm museum hours' in result.data['content']
    assert 'Walk from hotel' in result.data['content']
    assert 'estimated; verify' in result.data['content']


@pytest.mark.skip(reason="Legacy utility Skill retired from itinerary-first catalog")
@pytest.mark.asyncio
async def test_geocoding_flags_ambiguous_city():
    class Ambiguous(MappedProvider):
        async def request(self, capability, **kwargs):
            if capability == 'geocode':
                return ProviderResult({'results': [
                    {'name': 'Springfield', 'country': 'USA', 'latitude': 40, 'longitude': -80},
                    {'name': 'Springfield', 'country': 'Australia', 'latitude': -30, 'longitude': 151}]}, [])
            return await super().request(capability, **kwargs)
    result = await invoke('geocoding', {'place': 'Springfield'}, Ambiguous())
    assert result.data['requires_disambiguation']


@pytest.mark.skip(reason="Legacy utility Skill retired from itinerary-first catalog")
@pytest.mark.asyncio
async def test_hotel_ranked_by_neighborhood_but_availability_unverified():
    result = await invoke('hotel-search', {'latitude': 40, 'longitude': 20,
                                            'neighborhood': 'New Town'}, MappedProvider())
    assert result.data['results'][0]['name'] == 'Hotel B'
    assert result.data['results'][0]['availability'] == 'not_checked'


@pytest.mark.asyncio
async def test_place_discovery_respects_exclusions():
    result = await invoke('place-discovery', {'latitude': 40, 'longitude': 20,
                                             'exclude_names': ['Historic Tower']}, MappedProvider())
    assert [r['name'] for r in result.data['results']] == ['Craft Gallery']


@pytest.mark.skip(reason="Legacy utility Skill retired from itinerary-first catalog")
@pytest.mark.asyncio
async def test_walking_returns_accessibility_unknown_not_assured():
    result = await invoke('walking-directions', {'waypoints': [
        {'latitude': 40, 'longitude': 20}, {'latitude': 40.1, 'longitude': 20.1}]}, MappedProvider())
    assert result.data['estimated_minutes'] == 15
    assert result.data['step_free_status'] == 'not_checked'


@pytest.mark.asyncio
async def test_orchestrator_reuses_tourism_results_for_craft_experiences():
    provider = MappedProvider()
    controller = TravelSkillOrchestrator(skill_registry(), provider)
    result = await controller.candidate_context([{'city': 'Sample City', 'theme': 'Traditional craft'}],
                                                 request='Explore artisan workshops',
                                                 fields={'destination': 'Exampleland'})
    assert result['sample city']['experiences']
    assert [row[0] for row in provider.calls].count('places') == 1
    assert any(row['skill'] == 'cultural-experience-search' for row in controller.trace)


@pytest.mark.asyncio
async def test_orchestrator_budget_provides_allocation_not_a_quote():
    controller = TravelSkillOrchestrator(skill_registry(), None)
    budget = await controller.budget_context({'budget': 2400, 'currency': 'USD',
                                              'travelers': 2}, days=4)
    assert budget['verification_level'] == 'allocation_only'
    assert budget['daily_average'] == 600
    assert await controller.budget_context({'budget': 'unknown'}, 4) == {}


def test_exact_independent_skill_install():
    from maintenance.verify_legacy_skills import verify
    assert verify(ROOT) == (18, 18)


class SingleDayPlanner:
    def __init__(self):
        self.calls = []

    async def complete(self, role, system, prompt, json_mode=False):
        import json
        payload = json.loads(prompt)
        self.calls.append(payload)
        if 'allocated_days' not in payload:
            return {'blueprint': {'destination': 'Sample City', 'title': 'Sample trip',
                'summary': 'A craft and food visit', 'days': [{
                    'day': 1, 'city': 'Sample City', 'area': 'Old Town',
                    'theme': 'Traditional craft', 'anchors': ['Craft Gallery'],
                    'overnight': 'Sample City', 'transfer': '', 'day_type': 'sightseeing'}]}}
        return {'days': [{'day': 1, 'title': 'Craft and heritage',
                'area_cluster': 'Old Town', 'logistics': 'Walk between nearby stops.',
                'weather_alternative': 'Explore an indoor community gallery.',
                'activities': [
                    {'description': 'Explore Craft Gallery and local craft displays.',
                     'time': 'Morning', 'start_time': '09:00', 'duration_minutes': 90},
                    {'description': 'Try local cuisine at a restaurant near the gallery.',
                     'time': 'Lunch', 'start_time': '12:00', 'duration_minutes': 75},
                    {'description': 'Walk through the historic town center and exhibits.',
                     'time': 'Afternoon', 'start_time': '14:00', 'duration_minutes': 90}]}]}


@pytest.mark.asyncio
async def test_end_to_end_planner_receives_budget_and_cultural_skill_outputs():
    from planning.segmented_planning import generate_segmented_trip
    model = SingleDayPlanner()
    provider = MappedProvider()
    itinerary = await generate_segmented_trip(
        model, request='Plan a craft workshop visit and lunch.', destination='Sample City',
        days=1, fields={'destination': 'Sample City', 'country': 'Exampleland',
                         'budget': 1500, 'currency': 'USD', 'travelers': 2},
        preferences={}, registry=skill_registry(), providers=provider)
    assert len(itinerary['days']) == 1
    prompts = [item for item in model.calls if 'allocated_days' in item]
    assert len(prompts) == 1
    assert prompts[0]['specialist_budget_envelope']['daily_average'] == 1500
    assert prompts[0]['specialist_skill_candidates']['experiences']
    assert len([x for x in provider.calls if x[0] == 'places']) == 2


@pytest.mark.skip(reason="Legacy utility Skill retired from itinerary-first catalog")
@pytest.mark.asyncio
async def test_caller_label_cannot_fake_verified_cost():
    report = await invoke('travel-cost-estimation', {'items': [{
        'amount': 200, 'currency': 'JPY', 'basis': 'per_person',
        'verification_level': 'verified_by_provider',
        'verified_source': 'https://example.org/price'}]})
    assert report.data['unverified_indices'] == [0]
