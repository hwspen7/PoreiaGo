"""End-to-end regression cases from a real persisted, legacy-clock itinerary.

No model network, city-specific repair branches, or fabricated travel times.
"""
import json
from copy import deepcopy

import pytest

from aligo.activity_revision import merge_activity_patch
from aligo.memory import MemoryStore
from aligo.revision_planning import _generate_activity_patch, generate_revision
from aligo.trip_state import TripValidationError, validate_itinerary
from aligo.validation_gate import ItineraryGateError, explicit_revision_intent, validate_for_commit
from aligo.time_contract import activity_start, activity_start_minute


def legacy():
    return {'destination': 'Example City', 'title': 'City itinerary',
            'summary': 'Start at the Old Museum for a cultural morning.',
            'budget_strategy': 'Old Museum is the main admission cost.',
            'planning_assumptions': ['Visit the Old Museum first.'],
            'days': [{'day': 1, 'title': 'Old Museum and downtown',
                      'base_city': 'Example City',
                      'logistics': 'Travel from Old Museum to the waterfront.',
                      'activities': [
                          {'description': 'Visit the Old Museum', 'place_name': 'Old Museum',
                           'time': '09:00', 'duration_minutes': 180,
                           'activity_type': 'lodging', 'meal_type': 'breakfast'},
                          {'description': 'Explore the Old Town', 'place_name': 'Old Town',
                           'time': '13:00', 'duration_minutes': 90,
                           'activity_type': 'shopping', 'meal_type': 'lunch',
                           'transport_hint': 'From the Old Museum', 'transfer_minutes': 20},
                          {'description': 'Walk through the waterfront promenade',
                           'time': '15:30', 'duration_minutes': 120},
                          {'description': 'Dinner downtown', 'time': '19:00',
                           'duration_minutes': 120, 'activity_type': 'meal',
                           'meal_type': 'dinner'}]}]}


def patch(action, position, activity):
    return {'day': 1, 'operations': [{'action': action, 'position': position,
                                     'activity': activity}]}


def test_old_time_is_observable_without_rewriting_legacy_display():
    old = validate_itinerary(legacy())
    assert [activity_start(row) for row in old['days'][0]['activities']] == [
        '09:00', '13:00', '15:30', '19:00']
    assert old['days'][0]['activities'][0]['time'] == '09:00'
    assert 'start_time' not in old['days'][0]['activities'][0]
    assert activity_start_minute(old['days'][0]['activities'][3]) == 1140


def test_category_repair_for_misclassified_legacy_stop():
    activities = validate_itinerary(legacy())['days'][0]['activities']
    assert activities[0]['activity_type'] == 'attraction'
    assert activities[1]['activity_type'] == 'attraction'
    assert all('meal_type' not in x for x in activities[:3])
    assert activities[3]['meal_type'] == 'dinner'


def test_conflicting_dual_clock_is_rejected():
    source = legacy()
    source['days'][0]['activities'][0]['start_time'] = '10:00'
    with pytest.raises(TripValidationError, match='ACTIVITY_TIME_FIELDS_DISAGREE'):
        validate_itinerary(source)


def test_time_only_night_addition_satisfies_strict_constraint():
    old = validate_itinerary(legacy())
    new = merge_activity_patch(old, patch('insert', 4, {
        'description': 'Night arts district', 'place_name': 'Night Arts District',
        'time': '21:30', 'duration_minutes': 60}), day_number=1)
    validate_for_commit(new, previous=old, intent=explicit_revision_intent(
        'Add activities after 8pm'))


def test_moving_existing_dinner_does_not_count_as_night_addition():
    old = validate_itinerary(legacy())
    moved = deepcopy(old)
    moved['days'][0]['activities'][-1]['time'] = '21:30'
    with pytest.raises(ItineraryGateError) as error:
        validate_for_commit(moved, previous=old, intent=explicit_revision_intent(
            'Add activities after 8pm'))
    assert 'REVISION_ADDITION_NOT_MET' in {x.code for x in error.value.issues}


def test_superficial_museum_word_does_not_count_as_an_addition():
    old = validate_itinerary(legacy())
    changed = deepcopy(old)
    changed['days'][0]['activities'][0]['description'] = 'Visit the Old Museum and its museum galleries'
    with pytest.raises(ItineraryGateError):
        validate_for_commit(changed, previous=old,
                            intent=explicit_revision_intent('I want to visit more museums.'))


def test_null_duration_cannot_disable_a_scheduled_conflict():
    old = validate_itinerary(legacy())
    with pytest.raises(TripValidationError, match='REVISION_CANNOT_REMOVE_KNOWN_DURATION'):
        merge_activity_patch(old, patch('edit', 2, {'duration_minutes': None}), day_number=1)


def test_edit_of_existing_stop_cannot_bypass_venue_duplicate_check():
    old = validate_itinerary(legacy())
    with pytest.raises(TripValidationError, match='REVISION_DUPLICATE_NAMED_VENUE'):
        merge_activity_patch(old, patch('edit', 2, {
            'description': 'Explore the Old Museum', 'place_name': 'Old Museum'}), day_number=1)


def test_explicit_updated_transfer_survives_predecessor_replacement():
    old = validate_itinerary(legacy())
    proposal = {'day': 1, 'operations': [
        {'action': 'replace', 'position': 1, 'activity': {
            'description': 'Visit the New Museum', 'place_name': 'New Museum',
            'time': '09:00', 'duration_minutes': 180}},
        {'action': 'edit', 'position': 2, 'activity': {
            'transfer_minutes': 25, 'transport_hint': 'Travel from New Museum',
            'why_this_fits': 'The walk follows the visit'}}]}
    updated = merge_activity_patch(old, proposal, day_number=1)
    activity = updated['days'][0]['activities'][1]
    assert activity['transfer_minutes'] == 25
    assert 'New Museum' in activity['transport_hint']


def test_full_snapshot_no_reinheritance_after_dependency_cleanup(tmp_path):
    old = validate_itinerary(legacy())
    with MemoryStore(tmp_path / 'trip.sqlite3') as store:
        first = store.save_itinerary('u', 's', old, expected_version=0)
        revised = merge_activity_patch(old, patch('replace', 1, {
            'description': 'Visit the New Museum', 'place_name': 'New Museum',
            'time': '09:00', 'duration_minutes': 180}), day_number=1)
        assert 'summary' not in revised
        second = store.save_itinerary('u', 's', revised, expected_version=1,
                                      operation='revise')
        reread = store.active_itinerary('u', 's')
        assert first['version'] == 1 and second['version'] == 2
        assert 'summary' not in second['itinerary']
        assert 'summary' not in reread['itinerary']
        assert not any('Old Museum' in json.dumps(reread['itinerary'].get(f, ''))
                       for f in ('summary', 'budget_strategy', 'planning_assumptions'))


def test_new_create_in_existing_session_replaces_destination_atomically(tmp_path):
    old = validate_itinerary(legacy())
    new = deepcopy(old)
    new['destination'] = 'Other City'
    with MemoryStore(tmp_path / 'new.sqlite3') as store:
        store.save_itinerary('u', 's', old)
        state = store.save_itinerary('u', 's', new, expected_version=1, operation='create')
        assert state['version'] == 2
        assert store.active_itinerary('u', 's')['itinerary']['destination'] == 'Other City'
        assert store.itinerary_version_document('u', 's', 1)['destination'] == 'Example City'


def test_revision_diagnostics_are_scoped_and_bounded(tmp_path):
    with MemoryStore(tmp_path / 'diag.sqlite3') as store:
        for idx in range(36):
            store.record_revision_diagnostic('u', 's', baseline_version=1,
                                             stage='merge', details={'error': str(idx)})
        records = store.revision_diagnostics('u', 's')
        assert len(records) == 30
        assert records[0]['details']['error'] == '35'
        assert store.revision_diagnostics('other', 's') == []


@pytest.mark.asyncio
async def test_model_receives_legacy_times_and_recovers_from_bad_patch():
    old = validate_itinerary(legacy())
    class Model:
        def __init__(self):
            self.requests = []
        async def complete_revision_patch(self, system, prompt, *, day_number,
                                          response_schema, compact=False):
            data = json.loads(prompt)
            self.requests.append(data)
            if not compact:
                return {'day': 1, 'operations': [{
                    'action': 'insert', 'position': 1, 'activity': 'not-a-dict'}]}
            return patch('insert', 4, {
                'description': 'Late-night street art tour',
                'time': '21:30', 'duration_minutes': 60})
    model = Model()
    result = await _generate_activity_patch(model, old, old,
        'Add some activities after 8pm', day_number=1, fields={},
        preferences={}, evidence=[], timeout=5, enforce_intent=True)
    assert model.requests[0]['existing_activities'][0]['start_time'] == '09:00'
    assert model.requests[1]['previous_operations'][0]['description'] == ''
    assert len(result['days'][0]['activities']) == 5


@pytest.mark.asyncio
async def test_multiday_candidate_checked_after_all_edits():
    old = validate_itinerary({'destination': 'Example City', 'days': [
        {'day': 1, 'title': 'First day', 'base_city': 'Example City',
         'activities': [{'description': 'Visit the A Museum',
                         'place_name': 'A Museum', 'time': '10:00',
                         'duration_minutes': 60}]},
        {'day': 2, 'title': 'Second day', 'base_city': 'Example City',
         'activities': [{'description': 'Visit the B Museum',
                         'place_name': 'B Museum', 'time': '10:00',
                         'duration_minutes': 60}]}]})
    class Model:
        async def complete_revision_scope(self, system, prompt, *, response_schema):
            return {'target_days': [1, 2]}
        async def complete_revision_patch(self, system, prompt, *, day_number,
                                          response_schema, compact=False):
            if day_number == 1:
                return {'day': 1, 'operations': [{'action': 'replace', 'position': 1,
                    'activity': {'description': 'Visit the B Museum',
                                 'place_name': 'B Museum', 'time': '10:00',
                                 'duration_minutes': 60}}]}
            return {'day': 2, 'operations': [{'action': 'replace', 'position': 1,
                'activity': {'description': 'Visit the C Museum',
                             'place_name': 'C Museum', 'time': '10:00',
                             'duration_minutes': 60}}]}
    updated = await generate_revision(Model(), old,
       'Reallocate both days with new attractions', explicit_day=None, fields={},
       preferences={}, evidence=[], timeout=5)
    assert updated['days'][0]['activities'][0]['place_name'] == 'B Museum'
    assert updated['days'][1]['activities'][0]['place_name'] == 'C Museum'
