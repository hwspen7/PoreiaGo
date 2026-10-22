"""Behavioral regressions for offline gating and dependency-safe repairs."""
from copy import deepcopy
import pytest
from aligo.planning_checks import core_day_issues
from aligo.segmented_planning import _segment_issues, activity_repair_response_schema
from aligo.trip_state import validate_segment_days, TripValidationError
from aligo.content_audit import audit_itinerary
from aligo.traveler_review import review_traveler_quality


def day():
    return {'day': 1, 'title': 'Museum and garden', 'area_cluster': 'Downtown',
            'logistics': 'Walk between nearby downtown sites.',
            'weather_alternative': 'Replace the garden with the downtown art gallery.',
            'activities': [
                {'description': 'Visit the history museum', 'time': '09:00', 'duration_minutes': 120},
                {'description': 'Lunch at the covered market', 'time': '11:30', 'duration_minutes': 60,
                 'transfer_minutes': 20, 'buffer_minutes': 10},
                {'description': 'Explore the botanical garden', 'time': '13:00', 'duration_minutes': 90}]}


def test_incoming_transfer_and_buffer_are_included_without_skills():
    value = day()
    assert core_day_issues(value) == []
    value['activities'][1]['transfer_minutes'] = 25
    issues = _segment_issues([value], [{'day_type': 'sightseeing'}], request='Plan a day')
    assert any('transfer/buffer' in issue for issue in issues)


def test_partial_timeline_propagates_known_duration_but_not_unknown_duration():
    value = day()
    value['activities'][1]['time'] = 'Midday'
    value['activities'][1]['duration_minutes'] = 180
    assert core_day_issues(value)[0]['activity_index'] == 2
    del value['activities'][1]['duration_minutes']
    assert core_day_issues(value) == []


def test_cost_basis_checks_do_not_reject_explicitly_mixed_item_units():
    value = day()
    value['activities'][0].update(cost_estimate='100 JPY per person', cost_basis='per_person')
    value['activities'][1].update(cost_estimate='500 JPY per group', cost_basis='per_group')
    assert core_day_issues(value) == []
    value['activities'][0]['cost_basis'] = 'per_group'
    assert core_day_issues(value)[0]['code'] == 'cost_basis_conflict'


def test_transfer_metadata_survives_validation_and_rejects_invalid_values():
    value = day()
    normalized = validate_segment_days({'days': [value]}, first=1, last=1)[0]
    assert normalized['activities'][1]['transfer_minutes'] == 20
    for invalid in (-1, True, 2.5, 721):
        value['activities'][1]['transfer_minutes'] = invalid
        with pytest.raises(TripValidationError):
            validate_segment_days({'days': [value]}, first=1, last=1)


def test_bad_timeline_cannot_receive_full_timing_score_and_is_reported_once():
    value = day()
    good = audit_itinerary({'days': [value]})
    value['activities'][0]['duration_minutes'] = 180
    bad = audit_itinerary({'days': [value]})
    assert bad['dimensions']['pacing_and_timing'] <= 40
    assert bad['score'] < good['score']
    review = review_traveler_quality({'days': [value]})
    assert sum(i['code'] == 'activity_time_overlap' for i in review['issues']) == 1


def test_repair_contract_requires_refreshed_day_dependencies():
    schema = activity_repair_response_schema()
    assert 'day_updates' in schema['required']
    assert 'next_transport_hint' in schema['properties']['day_updates']['properties']


@pytest.mark.asyncio
async def test_offline_generation_retries_overlapping_day():
    from aligo.segmented_planning import generate_segmented_trip
    class Model:
        attempts = 0
        async def complete(self, role, system, prompt, **kwargs):
            return {'blueprint': {'destination': 'Example City', 'days': [
                {'day': 1, 'city': 'Example City', 'theme': 'History',
                 'overnight': 'Downtown', 'anchors': ['History museum']}]}}
        async def complete_daily_detail(self, system, prompt, **kwargs):
            self.attempts += 1
            value = day()
            if self.attempts == 1:
                value['activities'][0]['duration_minutes'] = 180
            return {'days': [value]}
    model = Model()
    result = await generate_segmented_trip(model, request='A day of history', destination='Example City',
                                          days=1, fields={}, preferences={})
    assert model.attempts == 2
    assert core_day_issues(result['days'][0]) == []


def test_clock_order_is_checked_even_without_durations():
    value = day()
    value['activities'][0].pop('duration_minutes')
    value['activities'][1]['time'] = '08:00'
    assert any(i['code'] == 'activity_time_order' for i in core_day_issues(value))


@pytest.mark.asyncio
async def test_online_discovery_requires_integrated_mode():
    from scripts.evaluate_travel_quality import run_models
    with pytest.raises(ValueError, match='integrated'):
        await run_models([], [], mode='model', online=True)
