"""Integration regressions for revised intent, timed editing and error continuity."""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from aligo.activity_revision import merge_activity_patch
from aligo.config import settings
from aligo.memory import MemoryStore
from aligo.registry import SkillRegistry
from aligo.revision_planning import generate_revision
from aligo.runtime import Runtime
from aligo.trip_state import TripValidationError, validate_itinerary
from aligo.validation_gate import ItineraryGateError, explicit_revision_intent, validate_for_commit

ROOT = Path(__file__).resolve().parents[1]


def itinerary():
    return validate_itinerary({'destination': 'Example City', 'title': 'Cultural day',
        'days': [{'day': 1, 'title': 'Places and dinner', 'activities': [
            {'description': 'Tour the old citadel', 'place_name': 'Old Citadel',
             'start_time': '09:00', 'duration_minutes': 180, 'area': 'Center'},
            {'description': 'Walk the public gardens', 'place_name': 'Public Gardens',
             'start_time': '13:30', 'duration_minutes': 90, 'area': 'Center'},
            {'description': 'Enjoy a traditional evening meal', 'place_name': 'Dinner Hall',
             'start_time': '19:00', 'duration_minutes': 120, 'area': 'Center'},
        ]}]})


class RepeatingModel:
    def __init__(self, *, patch):
        self.patch = patch
        self.calls = []

    async def complete_revision_patch(self, system, prompt, *, day_number, response_schema, compact=False):
        data = json.loads(prompt)
        self.calls.append(data)
        return self.patch


@pytest.mark.asyncio
async def test_museum_overlap_recovers_on_retry_without_losing_existing_visits():
    baseline = itinerary()
    model = RepeatingModel(patch={'day': 1, 'operations': [{'action': 'insert',
        'position': 2, 'activity': {'description': 'Explore the regional archives',
        'place_name': 'Regional Archives', 'venue_category': 'museum',
        'start_time': '13:30', 'duration_minutes': 60, 'area': 'Center'}}]})
    updated = await generate_revision(model, baseline, 'I want to visit more museums.',
                                      explicit_day=1, fields={}, preferences={}, evidence=[], timeout=10)
    assert len(model.calls) == 2
    assert 'prior_error' in model.calls[1]
    rows = updated['days'][0]['activities']
    assert len(rows) == 4
    assert [a['start_time'] for a in rows] == sorted(a['start_time'] for a in rows)
    assert next(a for a in rows if a.get('venue_category') == 'museum')['start_time'] == '15:15'
    assert rows[0]['description'] == baseline['days'][0]['activities'][0]['description']
    assert validate_for_commit(updated, previous=baseline,
                               intent=explicit_revision_intent('I want to visit more museums.'))


@pytest.mark.asyncio
async def test_after_time_request_adjusts_only_new_stop():
    model = RepeatingModel(patch={'day': 1, 'operations': [{'action': 'insert',
        'position': 3, 'activity': {'description': 'Visit the night market',
        'venue_category': 'nightlife', 'start_time': '20:00',
        'duration_minutes': 60, 'area': 'Center'}}]})
    result = await generate_revision(model, itinerary(), 'Add activities after 8pm.',
                                     explicit_day=1, fields={}, preferences={}, evidence=[], timeout=10)
    market = next(a for a in result['days'][0]['activities'] if 'night market' in a['description'])
    assert market['start_time'] > '20:00'
    assert len(model.calls) == 2


def test_wrong_replacement_target_rejected_by_structured_category():
    before = itinerary()
    before['days'][0]['activities'][0]['venue_category'] = 'museum'
    edited = merge_activity_patch(before, {'day': 1, 'operations': [{
        'action': 'replace', 'position': 2, 'activity': {'description': 'Visit a scenic riverside',
        'start_time': '13:30', 'duration_minutes': 90}}]}, day_number=1)
    with pytest.raises(ItineraryGateError, match='REVISION_REPLACEMENT_TARGET_NOT_MET'):
        validate_for_commit(edited, previous=before,
                            intent=explicit_revision_intent('Replace one museum with another attraction.'))


def test_cosmetic_edit_cannot_claim_less_rushed_schedule():
    before = itinerary()
    after = validate_itinerary({**before, 'summary': 'Much more relaxing.'})
    with pytest.raises(ItineraryGateError) as info:
        validate_for_commit(after, previous=before,
                            intent=explicit_revision_intent('Make the schedule less rushed.'))
    assert 'REVISION_PACE_NOT_IMPROVED' in [issue.code for issue in info.value.issues]


@pytest.mark.asyncio
async def test_unrecoverable_patch_returns_editable_existing_itinerary_without_fake_success(tmp_path):
    class MalformedModel:
        def configured(self, role):
            return True

        def router_decision_only(self):
            return True

        async def complete(self, role, system, prompt, json_mode=False, **kwargs):
            return {'operation': 'revise', 'mode': 'direct', 'itinerary': None,
                    'answer': '', 'fields': {}, 'goals': [], 'skills': []}

        async def complete_revision_scope(self, system, prompt, *, response_schema):
            return {'target_days': [1]}

        async def complete_revision_patch(self, system, prompt, *, day_number,
                                          response_schema, compact=False):
            return {'day': 1, 'operations': [{'action': 'remove', 'position': 999}]}

    store = MemoryStore(tmp_path / 'unchanged.sqlite3')
    initial = store.save_itinerary('u', 'j', itinerary())
    runtime = Runtime(store, MalformedModel(), object(),
                      SkillRegistry(ROOT / 'plugins/skills'),
                      replace(settings, agent_mode='model-first'), retrieval=object())
    response = await runtime.execute('u', 'j', 'Add more museums.')
    assert response['active_itinerary']['version'] == 1
    assert response['model_issue'] is None
    assert response['model_diagnostic']['revision_applied'] is False
    assert 'No new version was saved' in response['answer']
    assert 'Tour the old citadel' in response['answer']
    assert store.itinerary_versions('u', 'j')[0]['version'] == initial['version']
    store.close()


@pytest.mark.asyncio
async def test_four_attempt_cap_preserves_provisional_candidate_when_no_gap(monkeypatch):
    monkeypatch.setenv('ALIGO_REVISION_REPAIR_ATTEMPTS', '4')
    proposal = {'day': 1, 'operations': [{'action': 'insert', 'position': 1,
        'activity': {'description': 'Visit the metropolitan museum',
                     'venue_category': 'museum', 'start_time': '10:00',
                     'duration_minutes': 600}}]}
    model = RepeatingModel(patch=proposal)
    with pytest.raises(TripValidationError) as info:
        await generate_revision(model, itinerary(), 'I want to visit more museums.',
                                explicit_day=1, fields={}, preferences={}, evidence=[], timeout=10)
    assert len(model.calls) == 4
    assert info.value.draft_candidate is not None
    assert 'metropolitan museum' in str(info.value.draft_candidate)


def test_legacy_time_and_structured_venue_category_survive_normalization():
    old = itinerary()
    old['days'][0]['activities'][0].pop('start_time')
    old['days'][0]['activities'][0]['time'] = '09:00'
    model_addition = {'day': 1, 'operations': [{'action': 'insert', 'position': 2,
        'activity': {'description': 'Learn at the local archives',
                     'place_name': 'Local Archives', 'venue_category': 'museum',
                     'start_time': '16:00', 'duration_minutes': 60}}]}
    changed = merge_activity_patch(old, model_addition, day_number=1)
    assert changed['days'][0]['activities'][2]['venue_category'] == 'museum'
    assert validate_for_commit(changed, previous=old,
                               intent=explicit_revision_intent('I want to visit more museums.'))
