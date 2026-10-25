"""Preserve travel constraints while normalizing benign local-model outputs."""

from pathlib import Path

import pytest

from intelligence.intelligence import IntentFrame
from intelligence.model_agent import ITINERARY_REPAIR_SYSTEM
from services.registry import SkillRegistry
from core.runtime import Runtime
from planning.trip_state import TripValidationError, validate_itinerary
from core.config import settings

ROOT = Path(__file__).resolve().parents[2]


def example_itinerary(preferences):
    return {
        'destination': 'Japan',
        'title': 'Japan in five days',
        'days': [
            {'day': n, 'title': f'Day {n}',
             'activities': [{'description': 'Visit cultural sites'},
                            {'description': 'Taste local food'}]}
            for n in range(1, 6)
        ],
        'preferences': preferences,
    }


class RepairModel:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def router_decision_only(self):
        return True

    async def complete(self, role, system, prompt, json_mode=False):
        self.calls.append((role, system, prompt, json_mode))
        assert role == 'reasoning'
        return {'itinerary': self.response}


async def generate(preferences):
    model = RepairModel(example_itinerary(preferences))
    runtime = Runtime(None, model, None, SkillRegistry(ROOT / 'plugins/skills'),
                      settings, retrieval=object())
    frame = IntentFrame(fields={'destination': 'Japan', 'days': 5}, skill_calls=[],
                        mode='model_direct', task={'operation': 'create', 'itinerary': None})
    result = await runtime._prepare_trip_update(
        'Design a five-day trip in Japan with a relaxed pace.', [], None, '',
        frame, '', [], {}, frame.fields)
    return model, result[0]


@pytest.mark.asyncio
async def test_list_of_text_preferences_are_preserved_as_a_single_scalar():
    model, itinerary = await generate(['relaxed pace', 'cultural experiences', 'local food'])
    assert itinerary['preferences'] == {
        'travel_preferences': 'relaxed pace, cultural experiences, local food'}
    assert len(itinerary['days']) == 5
    assert len(model.calls) == 1


@pytest.mark.asyncio
async def test_string_preferences_are_preserved():
    _, itinerary = await generate('low-cost travel and local cuisine')
    assert itinerary['preferences'] == {'travel_preferences': 'low-cost travel and local cuisine'}


@pytest.mark.asyncio
async def test_list_of_one_key_objects_keeps_original_preference_keys():
    _, itinerary = await generate([{'pace': 'relaxed'}, {'budget': 'moderate'}])
    assert itinerary['preferences'] == {'pace': 'relaxed', 'budget': 'moderate'}


@pytest.mark.asyncio
async def test_name_value_pairs_are_converted_without_discarding_values():
    _, itinerary = await generate([{'name': 'interests', 'value': 'culture'},
                                   {'key': 'pace', 'value': 'relaxed'}])
    assert itinerary['preferences'] == {'interests': 'culture', 'pace': 'relaxed'}


@pytest.mark.asyncio
async def test_already_valid_flat_object_is_unchanged():
    _, itinerary = await generate({'pace': 'relaxed', 'budget': 'moderate'})
    assert itinerary['preferences'] == {'pace': 'relaxed', 'budget': 'moderate'}


@pytest.mark.asyncio
@pytest.mark.parametrize('invalid', [
    [{'pace': 'relaxed'}, {'pace': 'rushed'}],
    [{'interests': ['food', 'nature']}],
    [{'note': {'nested': 'value'}}],
    ['x' * 351],
    'x' * 351,
    list(range(3)),
    list(range(26)),
    {f'key{i}': 'value' for i in range(26)},
])
async def test_ambiguous_or_oversized_preferences_still_fail_strict_validation(invalid):
    with pytest.raises(TripValidationError, match='Preference'):
        await generate(invalid)


def test_strict_persistent_schema_has_not_been_relaxed():
    with pytest.raises(TripValidationError, match='Preferences must be a small object'):
        validate_itinerary(example_itinerary(['relaxed pace']))


def test_reasoning_prompt_requires_flat_scalar_preferences():
    assert 'MUST be a flat JSON object' in ITINERARY_REPAIR_SYSTEM
    assert 'Do not return preferences as an array' in ITINERARY_REPAIR_SYSTEM
