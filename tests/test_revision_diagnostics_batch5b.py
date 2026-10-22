"""General regression tests for edit identity, stale data and transfer boundaries."""

from copy import deepcopy

import pytest

from aligo.activity_revision import merge_activity_patch
from aligo.revision_consistency import cross_area_zero_gap_issues
from aligo.revision_planning import generate_revision
from aligo.trip_state import TripValidationError, validate_itinerary
from aligo.validation_gate import ItineraryGateError, validate_for_commit


def source_plan():
    return validate_itinerary({
        'destination': 'Example City',
        'title': 'Old Citadel and Valley Heights',
        'summary': 'A day featuring the Old Citadel and the Crystal Ridge.',
        'budget_strategy': 'The Crystal Ridge excursion dominates the budget.',
        'days': [{'day': 1, 'title': 'Old Citadel and Crystal Ridge',
                  'base_city': 'Example City',
                  'logistics': 'Use a hired car to reach the Crystal Ridge.',
                  'activities': [
                      {'description': 'Explore the Old Citadel and historic rooms.',
                       'start_time': '08:00', 'duration_minutes': 180,
                       'area': 'Old Quarter'},
                      {'description': 'Excursion to the Western section of the Crystal Ridge. Walk along the summit.',
                       'start_time': '12:00', 'duration_minutes': 240,
                       'area': 'Crystal Ridge'},
                      {'description': 'Enjoy dinner on the riverfront.',
                       'start_time': '18:00', 'duration_minutes': 90,
                       'area': 'Old Quarter', 'transport_hint': 'Return by taxi from the Crystal Ridge.'},
                  ]}]}, operation='create')


def test_removed_excursion_invalidates_stale_titles_and_copy():
    original = source_plan()
    proposal = merge_activity_patch(original, {'day': 1, 'operations': [
        {'action': 'replace', 'position': 2, 'activity': {
            'description': 'Visit the Botanical Garden and see rare plants.',
            'place_name': 'Botanical Garden', 'start_time': '12:00',
            'duration_minutes': 240, 'area': 'Old Quarter'}}
    ]}, day_number=1)
    assert 'summary' not in proposal
    assert 'budget_strategy' not in proposal
    assert 'Crystal Ridge' not in proposal['days'][0]['title']
    assert 'logistics' not in proposal['days'][0]
    assert 'transport_hint' not in proposal['days'][0]['activities'][2]
    assert validate_for_commit(proposal, previous=original)
    assert source_plan() == original


def test_duplicate_place_in_parenthesis_is_rejected_even_with_different_display_name():
    original = source_plan()
    with pytest.raises(TripValidationError, match='REVISION_DUPLICATE_NAMED_VENUE'):
        merge_activity_patch(original, {'day': 1, 'operations': [
            {'action': 'replace', 'position': 2, 'activity': {
                'description': 'Visit the Heritage Museum (Old Citadel) and a nearby gallery.',
                'start_time': '12:00', 'duration_minutes': 240,
                'area': 'Old Quarter'}}
        ]}, day_number=1)


def test_genuinely_distinct_stop_is_accepted():
    original = source_plan()
    proposal = merge_activity_patch(original, {'day': 1, 'operations': [
        {'action': 'replace', 'position': 2, 'activity': {
            'description': 'Visit the Heritage Museum and view regional collections.',
            'place_name': 'Heritage Museum', 'start_time': '12:00',
            'duration_minutes': 240, 'area': 'Old Quarter'}}
    ]}, day_number=1)
    assert validate_for_commit(proposal, previous=original)


def test_zero_gap_with_explicit_car_transfer_is_rejected_without_district_suffix():
    document = source_plan()
    day = deepcopy(document['days'][0])
    day['activities'][1]['start_time'] = '11:00'
    day['activities'][0]['duration_minutes'] = 180
    day['activities'][1]['transport_hint'] = 'Travel to the ridge by private car.'
    assert cross_area_zero_gap_issues(day)
    document['days'][0] = day
    with pytest.raises(ItineraryGateError) as raised:
        validate_for_commit(document)
    assert 'CROSS_AREA_TRANSFER_NO_GAP' in [issue.code for issue in raised.value.issues]


@pytest.mark.asyncio
async def test_time_constraints_provided_to_revision_model_as_structured_data():
    class Model:
        def __init__(self):
            self.context = None

        async def complete_revision_patch(self, system, prompt, *, day_number,
                                          response_schema, compact=False):
            import json
            self.context = json.loads(prompt)
            return {'day': day_number, 'operations': [
                {'action': 'insert', 'position': 3, 'activity': {
                    'description': 'Enjoy a concert at the Lantern Theater.',
                    'start_time': '20:45', 'duration_minutes': 60,
                    'area': 'Old Quarter'}}]}

    model = Model()
    output = await generate_revision(model, source_plan(),
                                     'Add one activity after 8pm', explicit_day=None,
                                     fields={}, preferences={}, evidence=[], timeout=10)
    assert model.context['observable_revision_constraints']['not_before_minute'] == 1200
    assert output['days'][0]['activities'][-1]['start_time'] == '20:45'
