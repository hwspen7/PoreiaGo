"""Regression tests for structured repair feedback after a duplicate venue edit."""

import json
from copy import deepcopy

import pytest

from aligo.activity_revision import merge_activity_patch
from aligo.revision_planning import generate_revision
from aligo.trip_state import TripValidationError, validate_itinerary


def itinerary():
    return validate_itinerary({
        'destination': 'Example City',
        'title': 'Historic Sites',
        'days': [{'day': 1, 'title': 'Historic Sites', 'base_city': 'Example City',
                  'activities': [
                      {'description': 'Explore the Old Citadel and historic rooms.',
                       'start_time': '08:00', 'duration_minutes': 180,
                       'area': 'Old Quarter', 'why_this_fits': 'Local history.'},
                      {'description': 'Excursion to the Crystal Ridge lookout.',
                       'start_time': '12:00', 'duration_minutes': 120,
                       'area': 'Old Quarter'},
                      {'description': 'Enjoy dinner near the riverside.',
                       'start_time': '18:00', 'duration_minutes': 90,
                       'area': 'Old Quarter'},
                  ]}]}, operation='create')


def proposal(repeated):
    desc = ('Visit the Heritage Museum (Old Citadel) and see historic displays.'
            if repeated else 'Visit the Regional Art Museum to see a new collection.')
    return {'day': 1, 'operations': [{'action': 'replace', 'position': 2,
                                     'activity': {'description': desc,
                                                  'start_time': '12:00',
                                                  'duration_minutes': 120,
                                                  'area': 'Old Quarter'}}]}


def test_duplicate_error_identifies_both_stops():
    with pytest.raises(TripValidationError, match='REVISION_DUPLICATE_NAMED_VENUE') as raised:
        merge_activity_patch(itinerary(), proposal(True), day_number=1)
    conflict = raised.value.revision_conflicts[0]
    assert conflict['proposed_position'] == 2
    assert conflict['existing_position'] == 1
    assert conflict['matched_alias'] == 'old citadel'
    assert 'Heritage Museum' in conflict['proposed_description']
    assert 'Old Citadel' in conflict['existing_description']


@pytest.mark.asyncio
async def test_revision_model_gets_detailed_retry_feedback_and_repairs():
    class Model:
        calls = []

        async def complete_revision_patch(self, system, prompt, *, day_number,
                                          response_schema, compact=False):
            payload = json.loads(prompt)
            self.calls.append((compact, payload))
            return proposal(repeated=not compact)

    model = Model()
    original = itinerary()
    revised = await generate_revision(
        model, original, 'I want to visit more museums.', explicit_day=None,
        fields={}, preferences={}, evidence=[], timeout=5)
    assert len(model.calls) == 2
    assert model.calls[0][0] is False
    compact, context = model.calls[1]
    assert compact is True
    conflict = context['previous_rejection']['duplicate_venues'][0]
    assert conflict['proposed_position'] == 2
    assert conflict['existing_position'] == 1
    assert context['previous_operations'][0]['description'].startswith('Visit the Heritage Museum')
    assert 'Regional Art Museum' in revised['days'][0]['activities'][1]['description']
    assert revised['days'][0]['activities'][0] == original['days'][0]['activities'][0]
    assert original == itinerary()


@pytest.mark.asyncio
async def test_twice_repeated_duplicate_still_fails_without_modifying_source():
    class Model:
        async def complete_revision_patch(self, system, prompt, *, day_number,
                                          response_schema, compact=False):
            return proposal(True)

    original = itinerary()
    snapshot = deepcopy(original)
    with pytest.raises(TripValidationError, match='REVISION_PATCH_INVALID.*REVISION_DUPLICATE_NAMED_VENUE'):
        await generate_revision(Model(), original, 'I want to visit more museums.',
                                explicit_day=None, fields={}, preferences={}, evidence=[], timeout=5)
    assert original == snapshot
