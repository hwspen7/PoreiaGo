"""Working drafts remain editable without certifying invalid schedules."""
import json
from dataclasses import replace
from pathlib import Path

import pytest

from aligo.config import settings
from aligo.memory import MemoryStore
from aligo.models import ModelUnavailable
from aligo.registry import SkillRegistry
from aligo.runtime import Runtime
from aligo.trip_state import validate_itinerary
from aligo.validation_gate import ItineraryGateError, validate_for_commit
from aligo.activity_revision import merge_activity_patch

ROOT = Path(__file__).resolve().parents[1]


def trip():
    return validate_itinerary({'destination': 'Example City', 'title': 'A cultural day',
        'days': [{'day': 1, 'title': 'Day 1', 'activities': [
            {'description': 'Explore historic citadel', 'time': '09:00', 'duration_minutes': 180},
            {'description': 'Explore local park', 'time': '13:30', 'duration_minutes': 90},
            {'description': 'Evening dinner', 'time': '19:00', 'duration_minutes': 120},
        ]}]})


def conflicting_candidate():
    return merge_activity_patch(trip(), {'day': 1, 'operations': [
        {'action': 'insert', 'position': 1, 'activity': {
            'description': 'Visit local museum', 'start_time': '10:00', 'duration_minutes': 60}}
    ]}, day_number=1, review_mode=True)


def test_working_draft_is_separate_persistent_and_owner_scoped(tmp_path):
    filename = tmp_path / 'journeys.sqlite3'
    store = MemoryStore(filename)
    verified = store.save_itinerary('a', 'main', trip())
    candidate = conflicting_candidate()
    with pytest.raises(ItineraryGateError):
        validate_for_commit(candidate)
    draft = store.save_working_itinerary('a', 'main', candidate, base_version=1,
        issues=[{'code': 'SCHEDULE_INVARIANT', 'day': 1}])
    assert draft['draft_version'] == 1
    assert store.active_itinerary('a', 'main') == verified
    assert store.working_itinerary('a', 'elsewhere') is None
    assert store.working_itinerary('b', 'main') is None
    store.close()
    store = MemoryStore(filename)
    assert store.working_itinerary('a', 'main')['itinerary'] == candidate
    assert store.working_itinerary('a', 'main')['issues'][0]['code'] == 'SCHEDULE_INVARIANT'
    assert len(store.itinerary_versions('a', 'main')) == 1
    store.close()


def test_working_draft_revision_and_verified_upgrade_are_atomic(tmp_path):
    store = MemoryStore(tmp_path / 'atomic.sqlite3')
    original = store.save_itinerary('a', 'session', trip())
    candidate = conflicting_candidate()
    store.save_working_itinerary('a', 'session', candidate, base_version=1)
    with pytest.raises(ValueError, match='Working draft changed'):
        store.save_working_itinerary('a', 'session', candidate, base_version=1,
                                     expected_draft_version=0)
    candidate['days'][0]['activities'][1]['start_time'] = '12:15'
    certified = store.save_itinerary('a', 'session', candidate, expected_version=1,
                                     operation='revise')
    assert certified['version'] == 2
    assert store.working_itinerary('a', 'session') is None
    assert len(store.itinerary_versions('a', 'session')) == 2
    assert original['version'] == 1
    store.close()


def test_bad_shapes_never_saved_as_drafts(tmp_path):
    store = MemoryStore(tmp_path / 'reject.sqlite3')
    with pytest.raises(ValueError):
        store.save_working_itinerary('a', 'b', {'days': 'invalid'}, base_version=0)
    assert store.working_itinerary('a', 'b') is None
    store.close()


def test_candidate_from_rejected_patch_is_still_structurally_valid():
    candidate = merge_activity_patch(trip(), {'day': 1, 'operations': [{
        'action': 'insert', 'position': 1, 'activity': {
            'description': 'Visit an evening lookout', 'duration_minutes': 60}}]},
        day_number=1, review_mode=True)
    assert len(candidate['days'][0]['activities']) == 4
    assert candidate['days'][0]['activities'][1]['description'] == 'Visit an evening lookout'
    with pytest.raises(ValueError):
        merge_activity_patch(trip(), {'day': 1, 'operations': [{
            'action': 'insert', 'position': 1, 'activity': {
                'description': 'Visit an evening lookout', 'duration_minutes': 60}}]},
            day_number=1)


class EditingModel:
    def __init__(self):
        self.prompts = []

    def configured(self, role):
        return role in {'router', 'reasoning', 'synthesis'}

    def router_decision_only(self):
        return True

    async def complete(self, role, system, prompt, json_mode=False, **kwargs):
        assert role == 'router'
        data = json.loads(prompt)
        self.prompts.append(('router', data))
        return {'operation': 'revise', 'mode': 'direct', 'itinerary': None,
                'answer': '', 'fields': {}, 'goals': [], 'skills': []}

    async def complete_revision_scope(self, system, prompt, *, response_schema):
        return {'target_days': [1]}

    async def complete_revision_patch(self, system, prompt, *, day_number,
                                      response_schema, compact=False):
        data = json.loads(prompt)
        self.prompts.append(('patch', data))
        if len(data['existing_activities']) == 3:
            return {'day': 1, 'operations': [{'action': 'insert', 'position': 1,
                'activity': {'description': 'Visit a new local museum',
                             'start_time': '10:00' if not compact else '10:30',
                             'duration_minutes': 500}}]}
        assert len(data['existing_activities']) == 4
        assert data['existing_activities'][1]['start_time'] in ('10:30', '10:00')
        return {'day': 1, 'operations': [{'action': 'edit', 'position': 2,
            'activity': {'start_time': '12:15', 'duration_minutes': 60}}]}


@pytest.mark.asyncio
async def test_real_runtime_can_resume_a_rejected_edit_from_draft(tmp_path):
    store = MemoryStore(tmp_path / 'runtime.sqlite3')
    store.save_itinerary('u', 'trip-a', trip())
    model = EditingModel()
    runtime = Runtime(store, model, object(),
                      SkillRegistry(ROOT / 'plugins' / 'skills'),
                      replace(settings, agent_mode='model-first'), retrieval=object())
    result1 = await runtime.execute('u', 'trip-a', 'Add another museum to this day.')
    assert result1['working_draft'] is not None
    assert result1['working_draft']['draft_version'] == 1
    assert 'Working draft' in result1['answer']
    assert result1['active_itinerary']['version'] == 1
    assert result1['trip_change'] is None
    result2 = await runtime.execute('u', 'trip-a', 'Move the new museum to a free time slot.')
    assert result2['working_draft'] is None
    assert result2['active_itinerary']['version'] == 2
    assert result2['trip_change']['changed_days'] == [1]
    assert store.working_itinerary('u', 'trip-a') is None
    assert len(store.itinerary_versions('u', 'trip-a')) == 2
    store.close()
