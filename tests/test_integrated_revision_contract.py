"""End-to-end regression cases for legacy clocks and snapshot persistence."""
from copy import deepcopy
import pytest

from aligo.memory import MemoryStore
from aligo.trip_state import TripValidationError, validate_itinerary
from aligo.validation_gate import validate_for_commit, explicit_revision_intent, ItineraryGateError
from aligo.activity_revision import merge_activity_patch
from aligo.time_contract import activity_start


def sample():
    return {'destination': 'Example City', 'title': 'Example trip',
            'summary': 'Enjoy the Old Museum and the river.',
            'days': [{'day': 1, 'title': 'Day One', 'activities': [
                {'description': 'Visit Old Museum', 'place_name': 'Old Museum', 'time': '09:00',
                 'duration_minutes': 120, 'activity_type': 'attraction'},
                {'description': 'Explore Riverside Gardens', 'place_name': 'Riverside Gardens',
                 'time': '12:00', 'duration_minutes': 90, 'activity_type': 'attraction'},
                {'description': 'Enjoy dinner', 'time': '18:00', 'duration_minutes': 120,
                 'activity_type': 'meal', 'meal_type': 'dinner'},
            ]}]}


def test_legacy_clock_visible_and_persisted():
    doc = validate_itinerary(sample())
    assert [activity_start(a) for a in doc['days'][0]['activities']] == ['09:00', '12:00', '18:00']
    assert all(activity_start(a) == a['time'] for a in doc['days'][0]['activities'])


def test_legacy_evening_passes_after_bound():
    prior = validate_itinerary(sample())
    updated = deepcopy(prior)
    updated['days'][0]['activities'].append({'description': 'Explore night market', 'time': '21:30',
                                           'duration_minutes': 60})
    result = validate_for_commit(updated, previous=prior,
                                intent=explicit_revision_intent('Add activities after 8pm'))
    assert activity_start(result['days'][0]['activities'][-1]) == '21:30'


def test_cleared_derived_summary_stays_cleared_through_storage(tmp_path):
    with MemoryStore(tmp_path / 'trip.db') as store:
        first = store.save_itinerary('u', 's', sample())
        changed = deepcopy(first['itinerary'])
        changed.pop('summary', None)
        changed['days'][0]['activities'][0]['description'] = 'Visit New Museum'
        changed['days'][0]['activities'][0]['place_name'] = 'New Museum'
        next_version = store.save_itinerary('u', 's', changed, expected_version=1, operation='revise')
        assert next_version['version'] == 2
        assert 'summary' not in store.active_itinerary('u','s')['itinerary']


def test_create_second_trip_does_not_become_revision(tmp_path):
    with MemoryStore(tmp_path / 'trip.db') as store:
        store.save_itinerary('u', 's', sample())
        second = sample()
        second['destination'] = 'New City'
        result = store.save_itinerary('u', 's', second, expected_version=1, operation='create')
        assert result['itinerary']['destination'] == 'New City'


def test_explicit_transport_edit_survives_dependency_reconciliation():
    baseline = validate_itinerary(sample())
    patch = {'day': 1, 'operations': [
        {'action': 'replace', 'position': 1, 'activity': {'description': 'Visit New Museum',
                                                        'place_name': 'New Museum', 'time': '09:00',
                                                        'duration_minutes': 120}},
        {'action': 'edit', 'position': 2, 'activity': {'transfer_minutes': 20}}
    ]}
    revised = merge_activity_patch(baseline, patch, day_number=1)
    assert revised['days'][0]['activities'][1]['transfer_minutes'] == 20


def test_cannot_null_existing_duration():
    baseline = validate_itinerary(sample())
    with pytest.raises(TripValidationError, match='REVISION_CANNOT_REMOVE_KNOWN_DURATION'):
        merge_activity_patch(baseline, {'day': 1, 'operations': [
            {'action': 'edit', 'position': 2, 'activity': {'duration_minutes': None}}
        ]}, day_number=1)


def test_invalid_activity_patch_is_safely_rejected():
    baseline = validate_itinerary(sample())
    with pytest.raises(TripValidationError, match='INVALID_ACTIVITY'):
        merge_activity_patch(baseline, {'day': 1, 'operations': [
            {'action': 'insert', 'position': 1, 'activity': 'Not an object'}
        ]}, day_number=1)


def test_conflicting_two_clock_fields_fail_closed():
    doc = sample()
    doc['days'][0]['activities'][0]['start_time'] = '10:00'
    with pytest.raises(TripValidationError, match='TIME_FIELDS_DISAGREE'):
        validate_itinerary(doc)


def test_changed_place_edit_detects_duplicates():
    baseline = validate_itinerary(sample())
    with pytest.raises(TripValidationError, match='DUPLICATE_NAMED_VENUE'):
        merge_activity_patch(baseline, {'day': 1, 'operations': [
            {'action': 'edit', 'position': 2, 'activity': {'description': 'Visit Old Museum',
                                                         'place_name': 'Old Museum'}}
        ]}, day_number=1)


def test_diagnostic_history_survives_storage(tmp_path):
    with MemoryStore(tmp_path / 'trip.db') as store:
        store.record_revision_diagnostic('u', 's', baseline_version=1,
                                         stage='precommit_validation',
                                         details={'issues': [{'code': 'REVISION_ACTIVITY_TIME_CONFLICT'}]})
        assert store.revision_diagnostics('u','s')[0]['details']['issues'][0]['code'] == 'REVISION_ACTIVITY_TIME_CONFLICT'
