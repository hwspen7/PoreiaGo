"""Validate local-model JSON wrapping and useful, privacy-safe failures."""

from dataclasses import replace
from pathlib import Path

import pytest

from core.config import settings
from intelligence.intelligence import IntentFrame
from services.registry import SkillRegistry
from core.runtime import Runtime
from planning.trip_state import TripValidationError

ROOT = Path(__file__).resolve().parents[2]


def valid_trip():
    return {
        'destination': 'Japan', 'title': 'A five-day cultural visit',
        'days': [
            {'day': day, 'title': f'Local exploration {day}',
             'activities': [{'description': 'Explore historic neighborhoods'},
                            {'description': 'Sample regional food'}]}
            for day in range(1, 6)
        ]
    }


class Model:
    def __init__(self, repair):
        self.repair = repair
        self.prompts = []

    async def complete(self, role, system, prompt, json_mode=False):
        self.prompts.append((role, prompt, json_mode))
        return self.repair


async def run_plan(candidate, repair):
    model = Model(repair)
    runtime = Runtime(None, model, None, SkillRegistry(ROOT / 'plugins/skills'),
                      replace(settings, agent_mode='model-first'), retrieval=object())
    frame = IntentFrame(fields={'destination': 'Japan', 'days': 5}, skill_calls=[],
                        mode='model_direct', task={'operation': 'create', 'itinerary': candidate})
    return model, await runtime._prepare_trip_update(
        'Design a five-day trip in Japan.', [], None, '', frame, '', [], {}, frame.fields)


@pytest.mark.asyncio
async def test_direct_router_itinerary_wrapper_does_not_need_repair():
    model, (itinerary, _, _) = await run_plan({'itinerary': valid_trip()}, None)
    assert itinerary['destination'] == 'Japan'
    assert len(itinerary['days']) == 5
    assert model.prompts == []


@pytest.mark.asyncio
async def test_repair_accepts_bare_itinerary_from_local_model():
    model, (itinerary, _, _) = await run_plan({'days': []}, valid_trip())
    assert len(itinerary['days']) == 5
    assert len(model.prompts) == 1
    assert 'initial_validation_error' in model.prompts[0][1]
    assert model.prompts[0][2] is True


@pytest.mark.asyncio
async def test_repair_accepts_wrapped_itinerary_from_local_model():
    _, (itinerary, _, _) = await run_plan(None, {'itinerary': valid_trip()})
    assert len(itinerary['days']) == 5


@pytest.mark.asyncio
async def test_failed_repair_reports_validation_reasons_not_raw_model_text():
    model = Model({'itinerary': {'destination': 'Japan', 'days': []}})
    runtime = Runtime(None, model, None, SkillRegistry(ROOT / 'plugins/skills'),
                      replace(settings, agent_mode='model-first'), retrieval=object())
    frame = IntentFrame(fields={'destination': 'Japan', 'days': 5}, skill_calls=[],
                        mode='model_direct', task={'operation': 'create', 'itinerary': None})
    with pytest.raises(TripValidationError, match=r'router=.*structured itinerary.*repair=.*1 to 30 days'):
        await runtime._prepare_trip_update(
            'Design a five-day trip in Japan.', [], None, '', frame, '', [], {}, frame.fields)


@pytest.mark.asyncio
async def test_partial_three_day_plan_is_repaired_to_five_requested_days():
    partial = valid_trip()
    partial['days'] = partial['days'][:3]
    model, (itinerary, _, _) = await run_plan(partial, {'itinerary': valid_trip()})
    assert len(itinerary['days']) == 5
    assert 'Expected 5 days' in model.prompts[0][1]
