"""General edit regressions: scope, budgets, preservation and atomic saves."""
import asyncio
import json
import sys
import types
from dataclasses import replace
from pathlib import Path

import pytest

from aligo.config import settings
from aligo.memory import MemoryStore
from aligo.registry import SkillRegistry
from aligo.models import ModelGateway, ModelUnavailable
from aligo.revision_planning import (
    day_schema, generate_revision, _selected_day_numbers)
from aligo.runtime import Runtime
from aligo.trip_state import TripValidationError, validate_itinerary

ROOT = Path(__file__).resolve().parents[1]


def seed(days=1):
    return validate_itinerary({'destination': 'Example City', 'title': 'A meaningful trip',
        'days': [{'day': n, 'title': f'Day {n} city culture',
                  'activities': [{'description': f'Historical attraction {n}',
                                  'start_time': '09:00', 'duration_minutes': 90},
                                 {'description': f'Art gallery {n}', 'start_time': '11:00',
                                  'duration_minutes': 90}],
                  'weather_alternative': 'Indoor cultural center'}
                 for n in range(1, days + 1)]})


class RevisionModel:
    def __init__(self, *, targets=None, fails=False, second_fails=False):
        self.calls = []
        self.targets = targets or [1]
        self.fails = fails
        self.second_fails = second_fails

    def configured(self, role):
        return role in {'router', 'reasoning', 'synthesis'}

    def router_decision_only(self):
        return True

    async def complete(self, role, system, prompt, json_mode=False, **kwargs):
        assert role == 'router', 'Incremental edits must not regenerate the full trip'
        self.calls.append(('router', json.loads(prompt)))
        return {'operation':'revise','mode':'direct','itinerary':None,'answer':'',
                'fields':{},'goals':[],'skills':[]}

    async def complete_revision_scope(self, system, prompt, *, response_schema):
        self.calls.append(('scope', json.loads(prompt)))
        return {'target_days': self.targets}

    async def complete_revision_day(self, system, prompt, *, day_number,
                                    response_schema, compact=False):
        p = json.loads(prompt)
        self.calls.append(('day', p, compact))
        if self.fails or (self.second_fails and day_number == 2):
            raise ModelUnavailable('ollama-reasoning: OUTPUT_TRUNCATED')
        assert response_schema['properties']['day']['enum'] == [day_number]
        assert 'days' not in p
        current = p['day_to_edit']
        return {'day': day_number, 'title': current['title'] + ' with museums',
                'activities': current['activities'] + [
                    {'description': f'Visit additional museum in district {day_number}',
                     'start_time': '15:00', 'duration_minutes': 60,
                     'why_this_fits': 'Adds cultural variety'}],
                'weather_alternative': current['weather_alternative']}


@pytest.mark.asyncio
async def test_one_day_edit_is_delta_with_no_extra_synthesis(tmp_path):
    store = MemoryStore(tmp_path / 'one.sqlite3')
    try:
        original = seed()
        store.save_itinerary('u', 'beijing', original)
        model = RevisionModel()
        registry = SkillRegistry(ROOT / 'plugins/skills')
        runtime = Runtime(store, model, object(), registry,
                          replace(settings, agent_mode='model-first'), retrieval=object())
        result = await runtime.execute('u', 'beijing', 'I want to visit more museums.')
        assert result['active_itinerary']['version'] == 2
        assert result['trip_change']['changed_days'] == [1]
        assert len(result['active_itinerary']['itinerary']['days'][0]['activities']) == 3
        assert [k[0] for k in model.calls] == ['router', 'day']
        assert 'additional museum' in result['answer']
        assert len(store.itinerary_versions('u', 'beijing')) == 2
    finally:
        store.close()


@pytest.mark.asyncio
async def test_multi_day_scope_only_modifies_selected_days():
    original = seed(11)
    model = RevisionModel(targets=[8, 3, 3])
    result = await generate_revision(model, original, 'Add museums where appropriate',
                                     explicit_day=None, fields={}, preferences={}, evidence=[],
                                     timeout=15)
    assert [row['day'] for row in result['days'] if row != original['days'][row['day'] - 1]] == [3, 8]
    assert [call[0] for call in model.calls] == ['scope', 'day', 'day']
    assert len(model.calls[0][1]['days']) == 11


@pytest.mark.asyncio
async def test_explicit_day_skips_scope_and_preserves_others():
    original = seed(11)
    model = RevisionModel(targets=[1, 2, 3])
    result = await generate_revision(model, original, 'Make Day 7 more cultural.',
                                     explicit_day=7, fields={}, preferences={}, evidence=[],
                                     timeout=15)
    assert [call[0] for call in model.calls] == ['day']
    assert result['days'][6] != original['days'][6]
    assert all(result['days'][i] == original['days'][i] for i in range(11) if i != 6)


@pytest.mark.asyncio
async def test_failed_day_does_not_commit_partial_multi_day_update(tmp_path):
    store = MemoryStore(tmp_path / 'rollback.sqlite3')
    original = seed(3)
    try:
        store.save_itinerary('u', 'journey-a', original)
        store.save_itinerary('u', 'journey-b', original)
        model = RevisionModel(targets=[1, 2], second_fails=True)
        runtime = Runtime(store, model, object(),
                          SkillRegistry(ROOT / 'plugins/skills'),
                          replace(settings, agent_mode='model-first'), retrieval=object())
        result = await runtime.execute('u', 'journey-a', 'Add museums across the journey.')
        assert result['trip_change'] is None
        assert result['active_itinerary']['version'] == 1
        assert store.active_itinerary('u', 'journey-a')['itinerary'] == original
        assert store.active_itinerary('u', 'journey-b')['itinerary'] == original
        assert len(store.itinerary_versions('u', 'journey-a')) == 1
        assert len([c for c in model.calls if c[0] == 'day']) == 3
    finally:
        store.close()


@pytest.mark.asyncio
async def test_invalid_day_can_recover_once_without_full_trip_rewrite():
    original = seed()

    class Recovering(RevisionModel):
        async def complete_revision_day(self, system, prompt, *, day_number,
                                        response_schema, compact=False):
            self.calls.append(('day', json.loads(prompt), compact))
            if not compact:
                return {'day': 1, 'title': 'Empty', 'activities': []}
            return {'day': 1, 'title': 'Museums', 'activities': ['Explore a museum']}

    model = Recovering()
    revised = await generate_revision(model, original, 'Add more museums',
                                      explicit_day=None, fields={}, preferences={}, evidence=[], timeout=15)
    assert revised['days'][0]['title'] == 'Museums'
    assert [call[2] for call in model.calls] == [False, True]


def test_scope_rejects_invalid_and_empty_days():
    for candidate in ({'target_days':[]}, {'target_days':[0]}, {'target_days':[True]},
                      {'target_days':[12]}, {'target_days':'all'}):
        with pytest.raises(TripValidationError):
            _selected_day_numbers(candidate, 11)


def test_day_schema_bounded_and_only_one_target():
    schema = day_schema(4)
    assert schema['properties']['day']['enum'] == [4]
    assert schema['properties']['activities']['maxItems'] == 12
    assert 'days' not in schema['properties']


@pytest.mark.asyncio
async def test_model_gateway_revision_requests_have_independent_budgets(tmp_path, monkeypatch):
    profile = {'profiles':[{'name':'local-reasoning','role':'reasoning',
        'provider':'openai-compatible','model':'local-test',
        'base_url':'http://127.0.0.1:11434/v1',
        'api_key_env':'ALIGO_OLLAMA_API_KEY','max_output_tokens':8192}]}
    file = tmp_path / 'models.json'
    file.write_text(json.dumps(profile))
    monkeypatch.setenv('ALIGO_OLLAMA_API_KEY','ollama')
    gateway = ModelGateway(replace(settings, models_file=file))
    seen = []

    class MockCompletions:
        async def create(self, **kwargs):
            seen.append(kwargs)
            from types import SimpleNamespace
            return SimpleNamespace(choices=[SimpleNamespace(finish_reason='stop',
                message=SimpleNamespace(content=json.dumps({'day':1,'title':'Museum day',
                    'activities':[{'description':'Visit an art museum'}]})))], usage=None)

    monkeypatch.setitem(sys.modules, 'openai', types.SimpleNamespace(AsyncOpenAI=object))
    gateway._clients['local-reasoning'] = type('Client',(),{
        'chat':type('Chat',(),{'completions':MockCompletions()})()})()
    for compact in (False, True, False):
        await gateway.complete_revision_day('system','{}', day_number=1,
                                            response_schema=day_schema(1), compact=compact)
    assert [row['max_tokens'] for row in seen] == [4096, 2048, 4096]
    assert all(row['response_format']['type'] == 'json_schema' for row in seen)
    assert all(row.get('extra_body',{}).get('reasoning_effort') == 'none' for row in seen)


@pytest.mark.asyncio
async def test_two_successive_edits_use_latest_saved_version(tmp_path):
    store = MemoryStore(tmp_path / 'followups.sqlite3')
    try:
        store.save_itinerary('u', 's', seed())
        class Chained(RevisionModel):
            async def complete_revision_day(self, system, prompt, *, day_number,
                                            response_schema, compact=False):
                data = json.loads(prompt)
                prior = data['day_to_edit']
                self.calls.append(('day', data, compact))
                activities = list(prior['activities']) + [{
                    'description': 'A new evening activity' if 'night' in data['request']
                    else 'A newly selected art museum',
                    'start_time': '21:00' if 'night' in data['request'] else '15:00',
                    'duration_minutes': 60}]
                return {'day': 1, 'title': prior['title'] + ' revised',
                        'activities': activities}
        model = Chained()
        runtime = Runtime(store, model, object(), SkillRegistry(ROOT / 'plugins/skills'),
                          replace(settings, agent_mode='model-first'), retrieval=object())
        first = await runtime.execute('u', 's', 'Add more museums.')
        second = await runtime.execute('u', 's', 'Add activities at night, after 8pm.')
        assert first['active_itinerary']['version'] == 2
        assert second['active_itinerary']['version'] == 3
        activities = second['active_itinerary']['itinerary']['days'][0]['activities']
        assert len(activities) == 4
        assert any('museum' in row['description'] for row in activities)
        assert activities[-1]['start_time'] == '21:00'
        assert len([c for c in model.calls if c[0] == 'day']) == 2
    finally:
        store.close()


@pytest.mark.asyncio
async def test_provider_unavailable_is_not_retried_for_an_identical_day():
    model = RevisionModel()
    async def fails(*args, **kwargs):
        model.calls.append(('day', None, kwargs.get('compact')))
        raise ModelUnavailable('connection failed')
    model.complete_revision_day = fails
    with pytest.raises(TripValidationError, match='MODEL_GENERATION_FAILED:MODEL_UNAVAILABLE'):
        await generate_revision(model, seed(), 'Add a different cultural visit',
                                explicit_day=1, fields={}, preferences={},
                                evidence=[], timeout=15)
    assert len(model.calls) == 1
