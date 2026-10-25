"""End-to-end revision repair contract for real-world repeated editing failures."""
from __future__ import annotations

import json

import pytest

from planning.activity_revision import merge_activity_patch
from planning.revision_planning import generate_revision
from planning.trip_state import TripValidationError, validate_itinerary
from planning.validation_gate import ItineraryGateError, explicit_revision_intent, validate_for_commit


def itinerary():
    return validate_itinerary({
        'destination': 'Example City',
        'days': [{'day': 1, 'title': 'Cultural itinerary', 'activities': [
            {'description': 'Explore the Old Citadel', 'time': '09:00', 'duration_minutes': 180},
            {'description': 'Walk through Riverside Park', 'time': '13:30', 'duration_minutes': 90},
            {'description': 'Enjoy a traditional dinner', 'time': '19:00', 'duration_minutes': 120},
        ]}]})


def test_topic_validation_gives_actionable_counts_and_preserves_strictness():
    original = itinerary()
    revised = merge_activity_patch(original, {'day': 1, 'operations': [{
        'action': 'replace', 'position': 2, 'activity': {
            'description': 'Visit a scenic garden', 'start_time': '13:30',
            'duration_minutes': 90}}]}, day_number=1)
    with pytest.raises(ItineraryGateError) as err:
        validate_for_commit(revised, previous=original,
                            intent=explicit_revision_intent('I want to visit more museums.'))
    issue = next(i for i in err.value.issues if i.code == 'REVISION_TOPIC_REQUIREMENT_NOT_MET')
    assert 'museum' in issue.detail
    assert 'before=0' in issue.detail
    assert 'after=0' in issue.detail


def test_missing_precise_time_reports_original_operation_and_field():
    original = itinerary()
    with pytest.raises(TripValidationError) as err:
        merge_activity_patch(original, {'day': 1, 'operations': [{
            'action': 'insert', 'position': 3, 'activity': {
                'description': 'Late art event', 'duration_minutes': 60}}]}, day_number=1)
    message = str(err.value)
    assert 'REVISION_NEW_ACTIVITY_MISSING_START_TIME' in message
    assert 'original_position=3' in message
    assert 'activity.start_time' in message


@pytest.mark.asyncio
async def test_missing_clock_retries_using_explicit_field_contract():
    class Mock:
        def __init__(self):
            self.requests = []

        async def complete_revision_patch(self, system, prompt, *, day_number,
                                          response_schema, compact=False):
            payload = json.loads(prompt)
            self.requests.append(payload)
            assert '24-hour HH:MM' in system
            if not compact:
                return {'day': 1, 'operations': [{
                    'action': 'insert', 'position': 3,
                    'activity': {'description': 'Night walking tour', 'duration_minutes': 60}}]}
            assert 'required_repair' in payload
            assert 'start_time' in payload['required_repair']
            return {'day': 1, 'operations': [{
                'action': 'insert', 'position': 3,
                'activity': {'description': 'Night walking tour', 'start_time': '21:30',
                             'duration_minutes': 60}}]}

    model = Mock()
    old = itinerary()
    updated = await generate_revision(model, old, 'Add activities after 8pm.',
                                      explicit_day=None, fields={}, preferences={},
                                      evidence=[], timeout=5)
    assert updated['days'][0]['activities'][-1]['start_time'] == '21:30'
    assert len(model.requests) == 2
    assert len(old['days'][0]['activities']) == 3


@pytest.mark.asyncio
async def test_topic_failure_feedback_guides_distinct_new_venue():
    class Mock:
        def __init__(self):
            self.requests = []

        async def complete_revision_patch(self, system, prompt, *, day_number,
                                          response_schema, compact=False):
            payload = json.loads(prompt)
            self.requests.append(payload)
            if not compact:
                return {'day': 1, 'operations': [{
                    'action': 'edit', 'position': 2,
                    'activity': {'why_this_fits': 'More time for cultural attractions'}}]}
            assert 'required_repair' in payload
            assert 'topic' in payload['required_repair']
            assert any('qualifying genuinely added stops=0' in issue['detail']
                       for issue in payload['previous_rejection']['issues'])
            return {'day': 1, 'operations': [{
                'action': 'replace', 'position': 2,
                'activity': {'description': 'Visit the Regional History Museum',
                             'start_time': '13:30', 'duration_minutes': 90}}]}

    old = itinerary()
    model = Mock()
    revised = await generate_revision(model, old, 'I want to visit more museums.',
                                     explicit_day=None, fields={}, preferences={},
                                     evidence=[], timeout=5)
    assert len(model.requests) == 2
    assert 'Regional History Museum' in revised['days'][0]['activities'][1]['description']
    assert old['days'][0]['activities'][1]['description'] == 'Walk through Riverside Park'
