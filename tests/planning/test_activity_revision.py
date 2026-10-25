"""Activity-level, lossless travel revision regression tests."""
import asyncio
import json
import sys
import types
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest

from planning.activity_revision import activity_patch_schema, merge_activity_patch
from intelligence.models import ModelGateway, ModelUnavailable
from planning.revision_planning import generate_revision
from planning.trip_state import TripValidationError, validate_itinerary, render_itinerary
from core.config import settings


def baseline(days=1):
    return validate_itinerary({
        'destination': 'Example City', 'title': 'Original itinerary',
        'summary': 'Original summary.',
        'days': [{'day': index, 'title': 'Day title',
                  'area_cluster': 'Historic center',
                  'base_city': 'Example City',
                  'weather_alternative': 'Original rainy day plan.',
                  'logistics': 'Original transport logistics.',
                  'budget_note': 'Keep costs low.',
                  'activities': [
                      {'description': 'Visit Palace Museum', 'start_time': '09:00',
                       'duration_minutes': 180, 'why_this_fits': 'History and art.',
                       'transport_hint': 'Subway to entrance.', 'booking_advice': 'Book online.',
                       'verification_needed': 'Confirm official hours.',
                       'cost_level': 'moderate', 'cost_estimate': 'Unknown',
                       'area': 'Central district', 'place_name': 'Palace Museum'},
                      {'description': 'Explore old streets', 'start_time': '12:30',
                       'duration_minutes': 120, 'why_this_fits': 'Local culture.',
                       'transport_hint': 'Walk across the old quarter.'},
                      {'description': 'Enjoy a historic temple', 'start_time': '15:00',
                       'duration_minutes': 120, 'why_this_fits': 'Traditional architecture.'},
                      {'description': 'Dinner', 'start_time': '18:30',
                       'duration_minutes': 90, 'why_this_fits': 'Local food.'}
                  ]} for index in range(1, days + 1)],
        'budget_strategy': 'Historical and cultural activities',
        'planning_assumptions': ['A full day of walking.']}, operation='create')


def add_night():
    return {'day': 1, 'operations': [
        {'action': 'insert', 'position': 4,
         'activity': {'description': 'Enjoy an illuminated pedestrian street',
                      'start_time': '20:30', 'duration_minutes': 60,
                      'why_this_fits': 'A short cultural evening walk.'}}]}


def test_lossless_activity_insert_and_rendering():
    original = baseline()
    updated = merge_activity_patch(original, add_night(), day_number=1)
    assert len(updated['days'][0]['activities']) == 5
    assert updated['days'][0]['activities'][:4] == original['days'][0]['activities']
    assert updated['days'][0]['weather_alternative'] == 'Original rainy day plan.'
    assert updated['days'][0]['logistics'] == 'Original transport logistics.'
    assert updated['summary'] == original['summary']
    text = render_itinerary(updated)
    assert 'Why: History and art.' in text
    assert 'Transfer: Subway to entrance.' in text
    assert 'Booking: Book online.' in text
    assert 'Cost level: moderate' in text
    assert '20:30' in text
    assert original == baseline()


def test_replace_only_selected_stop_and_preserve_other_stops():
    original = baseline()
    updated = merge_activity_patch(original, {'day': 1, 'operations': [
        {'action': 'replace', 'position': 2,
         'activity': {'description': 'Visit a museum for cultural collections',
                      'start_time': '12:30', 'duration_minutes': 90,
                      'why_this_fits': 'More museum time.'}}]}, day_number=1)
    assert updated['days'][0]['activities'][0] == original['days'][0]['activities'][0]
    assert updated['days'][0]['activities'][2:] == original['days'][0]['activities'][2:]
    assert 'museum for cultural' in updated['days'][0]['activities'][1]['description']


def test_edit_uses_original_positions_and_merges_instead_of_replacing():
    original = baseline()
    updated = merge_activity_patch(original, {'day': 1, 'operations': [
        {'action': 'edit', 'position': 2, 'activity': {'start_time': '13:15'}},
        {'action': 'insert', 'position': 1, 'activity': {
            'description': 'Short art gallery', 'start_time': '12:00',
            'duration_minutes': 60}},
        {'action': 'edit', 'position': 3, 'activity': {'start_time': '16:00'}}]}, day_number=1)
    assert updated['days'][0]['activities'][0] == original['days'][0]['activities'][0]
    assert updated['days'][0]['activities'][1]['description'] == 'Short art gallery'
    assert updated['days'][0]['activities'][2]['start_time'] == '13:15'
    assert updated['days'][0]['activities'][2]['why_this_fits'] == 'Local culture.'
    assert updated['days'][0]['activities'][3]['start_time'] == '16:00'


def test_unrelated_day_unchanged():
    original = baseline(3)
    updated = merge_activity_patch(original, {'day': 2, 'operations': [
        {'action': 'insert', 'position': 4, 'activity': {
            'description': 'Evening gallery', 'start_time': '20:30',
            'duration_minutes': 60}}]}, day_number=2)
    assert updated['days'][0] == original['days'][0]
    assert updated['days'][2] == original['days'][2]


@pytest.mark.parametrize('patch', [
    {'day': 1, 'operations': [{'action': 'insert', 'position': 0, 'activity': {'description': 'X', 'start_time': '08:00', 'duration_minutes': 90}}]},
    {'day': 1, 'operations': [{'action': 'insert', 'position': 4, 'activity': {'description': 'X', 'start_time': '19:00', 'duration_minutes': 90}}]},
    {'day': 1, 'operations': [{'action': 'insert', 'position': 3, 'activity': {'description': 'X'}}]},
    {'day': 1, 'operations': [{'action': 'edit', 'position': 1, 'activity': {'start_time': '11:30'}}]},
    {'day': 1, 'operations': [{'action': 'remove', 'position': 1}, {'action': 'edit', 'position': 1, 'activity': {'description': 'x'}}]},
    {'day': 1, 'operations': [{'action': 'replace', 'position': 9, 'activity': {'description': 'x'}}]},
    {'day': 1, 'operations': [{'action': 'insert', 'position': 4, 'activity': {'description': 'x', 'start_time': '20:30', 'duration_minutes': 60, 'evil': 'x'}}]},
])
def test_invalid_or_conflicting_patches_fail_atomically(patch):
    original = baseline()
    with pytest.raises(TripValidationError):
        merge_activity_patch(original, patch, day_number=1)
    assert original == baseline()


class PatchModel:
    def __init__(self, output=None, fail_first=False):
        self.output = output or add_night()
        self.calls = []
        self.fail_first = fail_first

    async def complete_revision_scope(self, system, prompt, *, response_schema):
        self.calls.append(('scope', json.loads(prompt)))
        return {'target_days': [1]}

    async def complete_revision_patch(self, system, prompt, *, day_number, response_schema, compact=False):
        self.calls.append(('patch', json.loads(prompt), compact))
        assert 'day_to_edit' not in json.loads(prompt)
        if self.fail_first and not compact:
            raise ModelUnavailable('ollama-reasoning: OUTPUT_TRUNCATED')
        return self.output


@pytest.mark.asyncio
async def test_patch_generation_does_not_regenerate_complete_day():
    source = baseline()
    model = PatchModel()
    result = await generate_revision(model, source, 'Add evening cultural activities',
                                     explicit_day=None, fields={}, preferences={},
                                     evidence=[], timeout=10)
    assert len(result['days'][0]['activities']) == 5
    assert len(model.calls) == 1
    assert model.calls[0][0] == 'patch'
    assert len(model.calls[0][1]['existing_activities']) == 4


@pytest.mark.asyncio
async def test_patch_retry_is_bounded_and_preserves_original():
    original = baseline()
    model = PatchModel(fail_first=True)
    result = await generate_revision(model, original, 'Add evening cultural activities',
                                     explicit_day=None, fields={}, preferences={},
                                     evidence=[], timeout=10)
    assert len(result['days'][0]['activities']) == 5
    assert len(model.calls) == 2
    assert [row[2] for row in model.calls] == [False, True]
    assert original == baseline()


@pytest.mark.asyncio
async def test_patch_gateway_uses_small_fresh_budgets(tmp_path, monkeypatch):
    config = {'profiles': [{'name': 'local-reasoning', 'role': 'reasoning',
                           'provider': 'openai-compatible', 'model': 'mock',
                           'base_url': 'http://127.0.0.1:11434/v1',
                           'api_key_env': 'POREIAGO_OLLAMA_API_KEY', 'max_output_tokens': 8192}]}
    file = tmp_path / 'models.json'
    file.write_text(json.dumps(config))
    monkeypatch.setenv('POREIAGO_OLLAMA_API_KEY', 'ollama')
    gateway = ModelGateway(replace(settings, models_file=file))
    seen = []
    class Responses:
        async def create(self, **kwargs):
            seen.append(kwargs)
            return types.SimpleNamespace(choices=[types.SimpleNamespace(finish_reason='stop',
                message=types.SimpleNamespace(content=json.dumps(add_night())))], usage=None)
    monkeypatch.setitem(sys.modules, 'openai', types.SimpleNamespace(AsyncOpenAI=object))
    gateway._clients['local-reasoning'] = types.SimpleNamespace(chat=types.SimpleNamespace(completions=Responses()))
    for compact in (False, True, False):
        await gateway.complete_revision_patch('system', '{}', day_number=1,
                                              response_schema=activity_patch_schema(1, 4),
                                              compact=compact)
    assert [row['max_tokens'] for row in seen] == [2048, 1536, 2048]
    assert all(row['response_format']['type'] == 'json_schema' for row in seen)

@pytest.mark.asyncio
async def test_runtime_two_edits_preserve_details_and_save_versions(tmp_path):
    from storage.memory import MemoryStore
    from services.registry import SkillRegistry
    from core.runtime import Runtime
    source = baseline()
    store = MemoryStore(tmp_path / 'atomic_updates.sqlite3')
    store.save_itinerary('u', 'trip-one', source)
    store.save_itinerary('u', 'trip-two', source)

    class Model(PatchModel):
        def configured(self, role):
            return role in {'router', 'reasoning', 'synthesis'}

        def router_decision_only(self):
            return True

        async def complete(self, role, system, prompt, json_mode=False, **kwargs):
            assert role == 'router'
            return {'operation': 'revise', 'mode': 'direct', 'itinerary': None,
                    'answer': '', 'fields': {}, 'goals': [], 'skills': []}

        async def complete_revision_patch(self, system, prompt, *, day_number,
                                          response_schema, compact=False):
            message = json.loads(prompt)['request']
            if 'museum' in message:
                return {'day': 1, 'operations': [{'action': 'replace', 'position': 2,
                    'activity': {'description': 'Visit an additional historical museum',
                                 'start_time': '12:30', 'duration_minutes': 90,
                                 'why_this_fits': 'More cultural collections.'}}]}
            return add_night()

    model = Model()
    try:
        runtime = Runtime(store, model, object(),
                          SkillRegistry(Path(__file__).resolve().parents[2] / 'plugins/skills'),
                          replace(settings, agent_mode='model-first'), retrieval=object())
        response1 = await runtime.execute('u', 'trip-one', 'Visit more museums.')
        response2 = await runtime.execute('u', 'trip-one', 'Add nighttime activities.')
        assert response1['active_itinerary']['version'] == 2
        assert response2['active_itinerary']['version'] == 3
        revised = response2['active_itinerary']['itinerary']
        assert len(revised['days'][0]['activities']) == 5
        assert 'additional historical museum' in revised['days'][0]['activities'][1]['description']
        assert revised['days'][0]['activities'][0] == source['days'][0]['activities'][0]
        assert 'Why: History and art.' in response2['answer']
        assert store.active_itinerary('u', 'trip-two')['itinerary'] == source
        assert len(store.itinerary_versions('u', 'trip-one')) == 3
    finally:
        store.close()


@pytest.mark.asyncio
async def test_invalid_patch_preserves_saved_version_and_has_specific_diagnostic(tmp_path):
    from storage.memory import MemoryStore
    from services.registry import SkillRegistry
    from core.runtime import Runtime
    original = baseline()
    store = MemoryStore(tmp_path / 'rollbacks.sqlite3')
    store.save_itinerary('u', 'trip', original)

    class BadModel(PatchModel):
        def configured(self, role):
            return role in {'router', 'reasoning', 'synthesis'}

        def router_decision_only(self):
            return True

        async def complete(self, role, system, prompt, json_mode=False, **kwargs):
            return {'operation': 'revise', 'mode': 'direct', 'itinerary': None,
                    'answer': '', 'fields': {}, 'goals': [], 'skills': []}

        async def complete_revision_patch(self, system, prompt, *, day_number,
                                          response_schema, compact=False):
            return {'day': 1, 'operations': [{'action': 'insert', 'position': 4,
                'activity': {'description': 'Impossible overlapping stop',
                             'start_time': '19:00', 'duration_minutes': 700}}]}

    try:
        runtime = Runtime(store, BadModel(), object(),
                          SkillRegistry(Path(__file__).resolve().parents[2] / 'plugins/skills'),
                          replace(settings, agent_mode='model-first'), retrieval=object())
        response = await runtime.execute('u', 'trip', 'Add an evening activity.')
        assert response['active_itinerary']['version'] == 1
        assert response['trip_change'] is None
        # The invalid candidate is now preserved only as a provisional working
        # draft, while the certified trip and version remain unchanged.
        assert response['model_diagnostic'] is None
        assert response['working_draft'] is not None
        assert response['working_draft']['status'] == 'draft'
        assert store.working_itinerary('u', 'trip') is not None
        assert store.active_itinerary('u', 'trip')['itinerary'] == original
    finally:
        store.close()
