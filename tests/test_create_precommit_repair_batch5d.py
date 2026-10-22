"""Creation paths repair bounded gate failures without weakening the commit gate."""
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import json

import pytest

from aligo.config import settings
from aligo.create_gate_repair import repair_created_itinerary
from aligo.intelligence import IntentFrame
from aligo.registry import SkillRegistry
from aligo.runtime import Runtime
from aligo.trip_state import validate_itinerary
from aligo.validation_gate import ItineraryGateError, validate_for_commit

ROOT = Path(__file__).resolve().parents[1]


def plan(days=1):
    return validate_itinerary({
        'destination': 'Example City', 'title': 'A sample journey',
        'days': [
            {'day': n, 'title': f'Day {n}', 'activities': [
                {'description': f'Visit a historic courtyard {n}',
                 'start_time': '09:00', 'duration_minutes': 120},
                {'description': f'Explore a craft center {n}',
                 'start_time': '12:00', 'duration_minutes': 90},
            ]} for n in range(1, days + 1)]
    })


def rejected(value):
    with pytest.raises(ItineraryGateError) as issue:
        validate_for_commit(value)
    return issue.value


class RepairModel:
    def __init__(self, *, fail=False, bad=False):
        self.calls = []
        self.fail = fail
        self.bad = bad

    async def complete_daily_detail(self, system, prompt, *, day_number, compact=False):
        data = json.loads(prompt)
        self.calls.append((day_number, data))
        if self.fail:
            raise ValueError('Invalid model JSON')
        day = deepcopy(data['existing_rejected_day'])
        for i, a in enumerate(day['activities']):
            a['start_time'] = ('09:00' if i == 0 else '12:00')
            a['duration_minutes'] = 120 if i == 0 else 90
        if self.bad:
            day['activities'][1]['start_time'] = '09:30'
        return {'days': [day]}


@pytest.mark.asyncio
async def test_one_day_overlap_gets_targeted_repair_and_revalidates():
    original = plan()
    original['days'][0]['activities'][1]['start_time'] = '10:00'
    snapshot = deepcopy(original)
    model = RepairModel()
    fixed = await repair_created_itinerary(
        model, original, rejected(original), request='A day in Example City',
        fields={'days': 1}, preferences={})
    assert validate_for_commit(fixed)
    assert fixed['days'][0]['activities'][1]['start_time'] == '12:00'
    assert original == snapshot
    assert len(model.calls) == 1
    assert model.calls[0][1]['validation_issues'][0]['code'] == 'SCHEDULE_INVARIANT'


@pytest.mark.asyncio
async def test_only_rejected_days_regenerated():
    original = plan(3)
    original['days'][1]['activities'][1]['start_time'] = '10:00'
    original_day1 = deepcopy(original['days'][0])
    original_day3 = deepcopy(original['days'][2])
    model = RepairModel()
    fixed = await repair_created_itinerary(
        model, original, rejected(original), request='Three days',
        fields={'days': 3}, preferences={})
    assert [number for number, _ in model.calls] == [2]
    assert fixed['days'][0] == original_day1
    assert fixed['days'][2] == original_day3
    assert validate_for_commit(fixed)


@pytest.mark.asyncio
async def test_untrusted_verified_claim_is_revoked_without_model_calls():
    original = plan()
    original['days'][0]['activities'][0].update({
        'evidence_status': 'verified', 'evidence_url': 'https://example.org/model-link'})
    model = RepairModel()
    fixed = await repair_created_itinerary(
        model, original, rejected(original), request='One day', fields={}, preferences={})
    assert fixed['days'][0]['activities'][0]['evidence_status'] == 'unverified'
    assert original['days'][0]['activities'][0]['evidence_status'] == 'verified'
    assert not model.calls


@pytest.mark.asyncio
async def test_repair_fails_closed_without_mutating_original():
    original = plan()
    original['days'][0]['activities'][1]['start_time'] = '10:00'
    old = deepcopy(original)
    with pytest.raises(ItineraryGateError) as error:
        await repair_created_itinerary(RepairModel(bad=True), original,
                                      rejected(original), request='Day trip',
                                      fields={}, preferences={})
    assert any(issue.code == 'SCHEDULE_INVARIANT' for issue in error.value.issues)
    assert original == old


@pytest.mark.asyncio
async def test_model_failure_retains_actionable_original_gate_error():
    original = plan()
    original['days'][0]['activities'][1]['start_time'] = '10:00'
    with pytest.raises(ItineraryGateError) as error:
        await repair_created_itinerary(RepairModel(fail=True), original,
                                      rejected(original), request='Day trip',
                                      fields={}, preferences={})
    assert error.value.issues[0].code == 'SCHEDULE_INVARIANT'


@pytest.mark.asyncio
async def test_create_runtime_invokes_gate_repair_before_returning():
    original = plan()
    original['days'][0]['activities'][1]['start_time'] = '10:00'
    model = RepairModel()
    runtime = Runtime(None, model, None, SkillRegistry(ROOT / 'plugins/skills'),
                      replace(settings, agent_mode='model-first'), retrieval=object())
    frame = IntentFrame(fields={'days': 1, 'destination': 'Example City'},
                        skill_calls=[])
    frame.task = {'operation': 'create', 'itinerary': original}
    output, _, _ = await runtime._prepare_trip_update(
        'One day in Example City', [], None, '', frame, None,
        [], {}, frame.fields)
    assert output['days'][0]['activities'][1]['start_time'] == '12:00'
    assert len(model.calls) == 1

@pytest.mark.asyncio
async def test_create_repairs_do_not_reuse_stale_router_narrative():
    original = plan()
    original['days'][0]['activities'][1]['start_time'] = '10:00'
    model = RepairModel()
    runtime = Runtime(None, model, None, SkillRegistry(ROOT / 'plugins/skills'),
                      replace(settings, agent_mode='model-first'), retrieval=object())
    frame = IntentFrame(fields={'days': 1, 'destination': 'Example City'},
                        skill_calls=[])
    frame.task = {'operation': 'create', 'itinerary': original}
    stale_router_answer = 'Day 1: The second visit begins at 10:00. ' * 12
    result, _, answer = await runtime._prepare_trip_update(
        'One day in Example City', [], None, '', frame, stale_router_answer,
        [], {}, frame.fields)
    assert result['days'][0]['activities'][1]['start_time'] == '12:00'
    assert '12:00' in answer
    assert 'second visit begins at 10:00' not in answer
