"""Behavioral regressions for cross-field dependencies and bounded revision repair."""
from copy import deepcopy

import pytest

from planning.activity_revision import activity_patch_schema, merge_activity_patch
from planning.revision_consistency import uncertain_area_transfers
from planning.revision_planning import generate_revision
from planning.trip_state import TripValidationError, validate_itinerary, render_itinerary
from planning.validation_gate import ItineraryGateError, validate_for_commit


def plan():
    return validate_itinerary({
        'destination': 'Example City',
        'title': 'Palace and neighborhoods',
        'summary': 'A day beginning with the Forbidden City and local culture.',
        'budget_strategy': 'The Forbidden City admission is the main expense.',
        'arrival_departure_plan': 'The day starts with the Forbidden City.',
        'days': [{'day': 1, 'title': 'Palace and neighborhoods',
                  'base_city': 'Example City',
                  'logistics': 'Arrive at the Forbidden City by subway.',
                  'weather_alternative': 'Visit the Forbidden City indoor halls.',
                  'budget_note': 'The Forbidden City has ticket requirements.',
                  'activities': [
                      {'description': 'Explore the Forbidden City and its courtyards.',
                       'start_time': '09:00', 'duration_minutes': 240,
                       'place_name': 'Forbidden City', 'area': 'North District',
                       'why_this_fits': 'Imperial architecture.',
                       'transport_hint': 'Use a subway line.',
                       'cost_estimate': '60 units'},
                      {'description': 'Enjoy the hilltop garden.',
                       'start_time': '13:30', 'duration_minutes': 90,
                       'area': 'North District',
                       'why_this_fits': 'A view over the palace you just walked through.',
                       'transport_hint': 'Walk from the Forbidden City North Gate.',
                       'cost_estimate': '2 units'},
                      {'description': 'Visit a historic street.',
                       'start_time': '15:30', 'duration_minutes': 120,
                       'area': 'North District',
                       'why_this_fits': 'Everyday cultural heritage.'},
                      {'description': 'Dinner near the old quarter.',
                       'start_time': '18:30', 'duration_minutes': 120,
                       'area': 'North District',
                       'why_this_fits': 'Local cuisine.'},
                  ]}]
    }, operation='create')


def test_removed_venue_no_longer_contaminates_derived_copy():
    source = plan()
    proposal = merge_activity_patch(source, {'day': 1, 'operations': [
        {'action': 'replace', 'position': 1, 'activity': {
            'description': 'Visit the Temple of Heaven.', 'place_name': 'Temple of Heaven',
            'area': 'South District', 'start_time': '09:00', 'duration_minutes': 180}}
    ]}, day_number=1)
    day = proposal['days'][0]
    assert day['activities'][1]['cost_estimate'] == '2 units'
    assert 'why_this_fits' not in day['activities'][1]
    assert 'transport_hint' not in day['activities'][1]
    assert 'logistics' not in day
    assert 'weather_alternative' not in day
    assert 'budget_note' not in day
    assert 'summary' not in proposal
    assert 'budget_strategy' not in proposal
    assert 'arrival_departure_plan' not in proposal
    assert 'Forbidden City' not in render_itinerary(proposal)
    assert source == plan()


def test_explicit_rewritten_trip_copy_preserved():
    revised = merge_activity_patch(plan(), {
        'day': 1,
        'operations': [{'action': 'replace', 'position': 1, 'activity': {
            'description': 'Visit the Temple of Heaven.', 'place_name': 'Temple of Heaven',
            'start_time': '09:00', 'duration_minutes': 180}}],
        'day_updates': {'logistics': 'Arrive at the Temple of Heaven by transit.'},
        'trip_updates': {'summary': 'A revised day visiting the Temple of Heaven.'}
    }, day_number=1)
    assert revised['summary'] == 'A revised day visiting the Temple of Heaven.'
    assert revised['days'][0]['logistics'] == 'Arrive at the Temple of Heaven by transit.'
    assert 'trip_updates' in activity_patch_schema(1, 4)['properties']


def test_recommendation_only_edit_retains_independent_evidence():
    source = plan()
    edited = merge_activity_patch(source, {'day': 1, 'operations': [
        {'action': 'edit', 'position': 1, 'activity': {'why_this_fits': 'Meaningful history.'}}
    ]}, day_number=1)
    assert edited['summary'] == source['summary']
    assert edited['days'][0]['activities'][0]['cost_estimate'] == '60 units'
    assert edited['days'][0]['activities'][1] == source['days'][0]['activities'][1]


def test_changed_inbound_origin_drops_stale_route_minutes():
    old = plan()
    old['days'][0]['activities'][1]['transfer_minutes'] = 15
    candidate = merge_activity_patch(old, {'day': 1, 'operations': [
        {'action': 'replace', 'position': 1, 'activity': {
            'description': 'Visit a nearby gallery', 'start_time': '09:00',
            'duration_minutes': 180}}
    ]}, day_number=1)
    following = candidate['days'][0]['activities'][1]
    assert 'transfer_minutes' not in following


def test_different_named_districts_cannot_have_zero_minute_transfers():
    old = plan()
    with pytest.raises(TripValidationError, match='REVISION_ACTIVITY_TIME_CONFLICT'):
        merge_activity_patch(old, {'day': 1, 'operations': [
            {'action': 'insert', 'position': 4, 'activity': {
                'description': 'Evening event', 'start_time': '20:30',
                'duration_minutes': 60, 'area': 'East District'}}]}, day_number=1)


def test_gap_does_not_certify_unobserved_transfer():
    old = plan()
    proposal = merge_activity_patch(old, {'day': 1, 'operations': [
        {'action': 'insert', 'position': 4, 'activity': {
            'description': 'Evening event', 'start_time': '20:45',
            'duration_minutes': 60, 'area': 'East District'}}]}, day_number=1)
    issues = uncertain_area_transfers(proposal)
    assert issues[0]['status'] == 'transfer_duration_unverified'
    assert issues[0]['available_minutes'] == 15
    assert validate_for_commit(proposal)


class RepairModel:
    def __init__(self):
        self.calls = []

    async def complete_revision_patch(self, system, prompt, *, day_number,
                                      response_schema, compact=False):
        import json
        request = json.loads(prompt)
        self.calls.append(request)
        if not compact:
            return {'day': 1, 'operations': [{'action': 'insert', 'position': 2,
                'activity': {'description': 'Visit another museum', 'start_time': '14:00',
                             'duration_minutes': 90}}]}
        assert 'REVISION_ACTIVITY_TIME_CONFLICT' in request['prior_error']
        assert request['previous_operations'][0]['action'] == 'insert'
        return {'day': 1, 'operations': [{'action': 'replace', 'position': 3,
            'activity': {'description': 'Visit another museum', 'start_time': '15:30',
                         'duration_minutes': 90}}]}


@pytest.mark.asyncio
async def test_failed_insertion_adapts_to_replacement_in_one_bounded_retry():
    model = RepairModel()
    result = await generate_revision(model, plan(), 'I want to visit more museums.',
                                     explicit_day=None, fields={}, preferences={},
                                     evidence=[], timeout=10)
    assert len(model.calls) == 2
    assert result['days'][0]['activities'][2]['description'] == 'Visit another museum'
    assert len(result['days'][0]['activities']) == 4


@pytest.mark.asyncio
async def test_two_invalid_patches_preserve_original():
    class AlwaysInvalid(RepairModel):
        async def complete_revision_patch(self, system, prompt, *, day_number,
                                          response_schema, compact=False):
            return {'day': 1, 'operations': [{'action': 'insert', 'position': 2,
                'activity': {'description': 'Visit another museum', 'start_time': '14:00',
                             'duration_minutes': 90}}]}
    source = plan()
    with pytest.raises(TripValidationError, match='REVISION_PATCH_INVALID'):
        await generate_revision(AlwaysInvalid(), source, 'I want to visit more museums.',
                                explicit_day=None, fields={}, preferences=[],
                                evidence=[], timeout=10)
    assert source == plan()


def test_other_revision_entry_cannot_persist_stale_old_route():
    original = plan()
    new = deepcopy(original)
    new['days'][0]['activities'][0] = {
        'description': 'Visit the Temple of Heaven', 'place_name': 'Temple of Heaven',
        'start_time': '09:00', 'duration_minutes': 180}
    with pytest.raises(ItineraryGateError) as raised:
        validate_for_commit(new, previous=original)
    assert 'STALE_REVISION_REFERENCE' in [issue.code for issue in raised.value.issues]


def test_a_view_of_removed_venue_is_not_itself_an_invalid_reference():
    original = plan()
    new = merge_activity_patch(original, {'day': 1, 'operations': [
        {'action': 'replace', 'position': 1, 'activity': {
            'description': 'Visit the Temple of Heaven', 'place_name': 'Temple of Heaven',
            'start_time': '09:00', 'duration_minutes': 180}},
        {'action': 'edit', 'position': 2, 'activity': {
            'description': 'Enjoy distant views of the Forbidden City from a garden.'}}
    ]}, day_number=1)
    assert 'Forbidden City' in new['days'][0]['activities'][1]['description']
    assert validate_for_commit(new, previous=original)
