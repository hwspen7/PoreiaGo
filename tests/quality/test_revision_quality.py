"""Regression coverage for real multi-turn timing and intention failures.

All names are synthetic. No destination-specific rule is required.
"""
from __future__ import annotations

from copy import deepcopy

import pytest

from planning.revision_resilience import reconcile_schedule
from planning.trip_state import validate_itinerary
from planning.validation_gate import (
    ItineraryGateError, explicit_revision_intent, validate_for_commit,
)
from planning.working_drafts import review_draft


def itinerary():
    return validate_itinerary({
        'destination': 'Example City', 'title': 'Example itinerary',
        'days': [{'day': 1, 'title': 'Historic center', 'activities': [
            {'description': 'Explore the Royal Museum', 'place_name': 'Royal Museum',
             'venue_category': 'museum', 'start_time': '09:00',
             'duration_minutes': 180, 'area': 'Old Town'},
            {'description': 'Walk the River Gardens', 'place_name': 'River Gardens',
             'venue_category': 'park', 'start_time': '13:00',
             'duration_minutes': 90, 'area': 'Old Town'},
            {'description': 'Enjoy a local dinner', 'place_name': 'Dinner Hall',
             'activity_type': 'meal', 'meal_type': 'dinner',
             'start_time': '19:00', 'duration_minutes': 120, 'area': 'Old Town'},
        ]}]})


def intent_check(doc, prior, request):
    return validate_for_commit(doc, previous=prior, intent=explicit_revision_intent(request))


def issue_codes(exc):
    return {item.code for item in exc.value.issues}


def test_early_parks_request_is_a_structured_constraint():
    intent = explicit_revision_intent('I want to visit some parks earlier, around 6am.')
    assert intent['early_topic'] == 'park'
    assert intent['early_target_minute'] == 360
    assert intent['requires_activity_change']


def test_early_park_cannot_be_satisfied_by_note_or_other_venue():
    prior = itinerary()
    changed = deepcopy(prior)
    changed['days'][0]['activities'][0]['start_time'] = '06:00'
    changed['days'][0]['notes'] = 'Morning parks added.'
    with pytest.raises(ItineraryGateError) as rejected:
        intent_check(changed, prior, 'I want to visit some parks earlier, around 6am.')
    assert 'REVISION_EARLY_TOPIC_NOT_MET' in issue_codes(rejected)


def test_moving_existing_park_to_morning_counts_as_real_change():
    prior = itinerary()
    updated = deepcopy(prior)
    park = updated['days'][0]['activities'].pop(1)
    park['start_time'] = '06:00'
    updated['days'][0]['activities'].insert(0, park)
    assert intent_check(updated, prior, 'I want to visit some parks earlier, around 6am.')


def test_early_park_does_not_have_to_be_a_new_venue():
    prior = itinerary()
    updated = deepcopy(prior)
    park = updated['days'][0]['activities'].pop(1)
    park['start_time'] = '06:30'
    updated['days'][0]['activities'].insert(0, park)
    assert intent_check(updated, prior, 'I want to visit some parks earlier, around 6am.')


def test_exact_named_visit_is_checked_in_actual_activity_list():
    request = 'I want to go to Harbor Square at 4am to watch an event.'
    intent = explicit_revision_intent(request)
    assert intent['anchor_place'] == 'Harbor Square'
    assert intent['anchor_minute'] == 240
    prior = itinerary()
    unchanged = deepcopy(prior)
    unchanged['days'][0]['notes'] = 'Visit Harbor Square at 04:00.'
    with pytest.raises(ItineraryGateError) as rejected:
        intent_check(unchanged, prior, request)
    assert 'REVISION_ANCHORED_VISIT_NOT_MET' in issue_codes(rejected)
    updated = deepcopy(prior)
    updated['days'][0]['activities'].insert(0, {
        'description': 'Watch the ceremony at Harbor Square',
        'place_name': 'Harbor Square', 'start_time': '04:00',
        'duration_minutes': 60, 'area': 'Old Town'})
    assert intent_check(updated, prior, request)


def test_one_hour_span_shrink_of_long_day_is_not_real_pace_relief():
    prior = itinerary()
    prior['days'][0]['activities'][0]['start_time'] = '04:00'
    prior['days'][0]['activities'][2]['start_time'] = '22:00'
    changed = deepcopy(prior)
    changed['days'][0]['activities'][2]['start_time'] = '21:00'
    with pytest.raises(ItineraryGateError) as rejected:
        intent_check(changed, prior, 'Make the schedule less rushed.')
    assert 'REVISION_PACE_NOT_IMPROVED' in issue_codes(rejected)


def test_real_stop_removal_with_substantive_dwell_reduction_meets_pace_request():
    prior = itinerary()
    changed = deepcopy(prior)
    changed['days'][0]['activities'].pop(1)
    assert intent_check(changed, prior, 'Make the schedule less rushed.')


def test_replacing_short_stop_but_no_dwell_reduction_is_not_enough():
    prior = itinerary()
    changed = deepcopy(prior)
    changed['days'][0]['activities'].pop(1)
    changed['days'][0]['activities'][0]['duration_minutes'] += 90
    with pytest.raises(ItineraryGateError) as rejected:
        intent_check(changed, prior, 'Make the schedule less rushed.')
    assert 'REVISION_PACE_NOT_IMPROVED' in issue_codes(rejected)


def test_scheduler_respects_explicit_incoming_transfer_and_margin():
    day = {'day': 1, 'title': 'Route', 'activities': [
        {'description': 'Tour North Hall', 'place_name': 'North Hall',
         'start_time': '09:00', 'duration_minutes': 120, 'area': 'North'},
        {'description': 'Tour Central Gallery', 'place_name': 'Central Gallery',
         'start_time': '09:30', 'duration_minutes': 60, 'area': 'South',
         'transfer_minutes': 45, 'buffer_minutes': 15},
        {'description': 'Meet for dinner', 'place_name': 'Dinner',
         'start_time': '16:00', 'duration_minutes': 90, 'area': 'South'},
    ]}
    adjusted, origins = reconcile_schedule(day, [1, None, 2])
    added = next(a for a in adjusted['activities'] if a.get('place_name') == 'Central Gallery')
    assert added['start_time'] == '12:00'
    assert added['transfer_minutes'] == 45
    assert added['buffer_minutes'] == 15
    assert origins == [1, None, 2]


def test_draft_downgrades_false_verified_claim_before_collecting_issues():
    candidate = itinerary()
    candidate['days'][0]['activities'][0]['evidence_status'] = 'verified'
    candidate['days'][0]['activities'][0]['evidence_url'] = 'https://example.org/source'
    reviewed, issues = review_draft(candidate)
    assert reviewed['days'][0]['activities'][0]['evidence_status'] == 'unverified'
    assert not any(row['code'] == 'UNTRUSTED_VERIFIED_EVIDENCE' for row in issues)


def test_draft_reordering_invalidates_stale_inbound_route():
    candidate = itinerary()
    candidate['days'][0]['activities'][1]['start_time'] = '16:00'
    candidate['days'][0]['activities'][2]['start_time'] = '14:00'
    candidate['days'][0]['activities'][2]['transport_hint'] = 'Taxi from River Gardens'
    candidate['days'][0]['activities'][2]['transfer_minutes'] = 20
    reviewed, issues = review_draft(candidate)
    assert [a['start_time'] for a in reviewed['days'][0]['activities']] == ['09:00', '14:00', '16:00']
    dinner = reviewed['days'][0]['activities'][1]
    assert 'transport_hint' not in dinner
    assert 'transfer_minutes' not in dinner
    assert any(row['code'] == 'DRAFT_ACTIVITY_ORDER_RECONCILED' for row in issues)


def test_legacy_intents_and_ordinary_slower_pace_are_not_overconstrained():
    assert explicit_revision_intent('I want to visit more museums.')['requested_term'] == 'museum'
    assert explicit_revision_intent('Add activities after 20:00.')['not_before_minute'] == 1200
    assert not explicit_revision_intent('Revise Day 7 for more art and a slower pace.').get('less_rushed')
