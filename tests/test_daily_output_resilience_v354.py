"""Bounded daily output, a different truncation repair, and intact quality gates."""
from __future__ import annotations

import json
import sys
import types
from types import SimpleNamespace

import pytest

from aligo.models import ModelGateway, ModelUnavailable
from aligo.segmented_planning import (
    DAY_COMPACT_SYSTEM, DAY_SYSTEM, daily_response_schema,
    generate_daily_trip,
)
from aligo.trip_state import TripValidationError


def _blueprint(days):
    return {'blueprint': {
        'destination': 'Japan', 'title': 'Japan culture and food',
        'summary': 'A coherent route based on Kyoto.',
        'days': [
            {'day': n, 'city': 'Kyoto', 'theme': 'Neighborhood culture',
             'overnight': 'Kyoto Station', 'area': 'Central Kyoto',
             'anchors': [f'Local cultural experience {n}'], 'day_type': 'sightseeing',
             'transfer': 'Local transport within Kyoto'}
            for n in range(1, days+1)],
    }}


def _single_day(number):
    return {'days': [{'day': number, 'title': 'Traditional Kyoto',
        'activities': [
            {'description': f'Explore a distinct local cultural stop {number}', 'time': 'Morning'},
            {'description': 'Taste Kyoto tofu dishes for lunch', 'time': 'Afternoon'},
            {'description': f'Stroll through a different area {number}', 'time': 'Evening'},
        ], 'area_cluster': 'Central Kyoto',
        'logistics': 'Use subway between Kyoto Station and Shijo.',
        'weather_alternative': 'Replace an outdoor walk with the Kyoto Museum of Crafts and Design.'}]}


class CappedModel:
    def __init__(self, always_fail=False):
        self.calls = []
        self.always_fail = always_fail
        self.day_tries = {}

    async def complete(self, role, system, prompt, json_mode=False):
        assert json_mode and role == 'reasoning'
        request = json.loads(prompt)
        assert 'day_number' not in request
        self.calls.append(('blueprint', request))
        return _blueprint(request['days'])

    async def complete_daily_detail(self, system, prompt, *, day_number, compact=False):
        data = json.loads(prompt)
        self.calls.append(('day', day_number, compact, system, data))
        self.day_tries[day_number] = self.day_tries.get(day_number, 0) + 1
        if day_number == 2 and (self.always_fail or self.day_tries[day_number] == 1):
            raise ModelUnavailable('ollama-reasoning: OUTPUT_TRUNCATED')
        return _single_day(day_number)


@pytest.mark.asyncio
async def test_retry_is_compact_and_restricted_to_failed_day():
    model = CappedModel()
    trip = await generate_daily_trip(model, request='Eleven days of food and culture in Japan.',
        destination='Japan', days=11, fields={'days': 11}, preferences={})
    assert len(trip['days']) == 11
    assert model.day_tries[2] == 2
    assert all(model.day_tries[n] == 1 for n in range(1, 12) if n != 2)
    retries = [row for row in model.calls if row[0] == 'day' and row[1] == 2]
    assert [row[2] for row in retries] == [False, True]
    assert retries[0][3] == DAY_SYSTEM
    assert retries[1][3] == DAY_COMPACT_SYSTEM
    assert retries[1][4]['task'] == 'repair_one_day'
    assert 'OUTPUT_TRUNCATED' in retries[1][4]['revision_issues'][0]


@pytest.mark.asyncio
async def test_multiple_truncations_fail_closed_without_persisting_partial_plan():
    model = CappedModel(always_fail=True)
    with pytest.raises(TripValidationError, match=r'Detail day 2 .*OUTPUT_TRUNCATED'):
        await generate_daily_trip(model, request='Eleven days of food and culture in Japan.',
            destination='Japan', days=11, fields={}, preferences={})
    assert model.day_tries == {1: 1, 2: 2}


def test_daily_schema_restricts_count_and_preserves_useful_detail():
    schema = daily_response_schema(7)
    assert schema['properties']['days']['maxItems'] == 1
    day = schema['properties']['days']['items']
    assert day['properties']['day']['enum'] == [7]
    activity = day['properties']['activities']['items']['properties']
    for key in ('description', 'time', 'area', 'why_this_fits',
                'transport_hint', 'booking_advice', 'cost_estimate',
                'verification_needed'):
        assert key in activity
    for key in ('logistics', 'weather_alternative', 'area_cluster'):
        assert key in day['properties']
    assert day['properties']['activities']['maxItems'] >= 4


@pytest.mark.asyncio
async def test_ollama_uses_schema_and_budget_only_for_daily_calls(monkeypatch, tmp_path):
    captured = []

    class StubCompletions:
        async def create(self, **kwargs):
            captured.append(kwargs)
            content = _single_day(2) if kwargs.get('response_format', {}).get('type') == 'json_schema' else _blueprint(3)
            return SimpleNamespace(choices=[SimpleNamespace(
                finish_reason='stop', message=SimpleNamespace(content=json.dumps(content)))])

    class StubAsyncOpenAI:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=StubCompletions())

    monkeypatch.setitem(sys.modules, 'openai', types.SimpleNamespace(AsyncOpenAI=StubAsyncOpenAI))
    monkeypatch.setenv('ALIGO_OLLAMA_API_KEY', 'ollama')
    cfg = tmp_path / 'models.json'
    cfg.write_text(json.dumps({'profiles': [{'name':'ollama-reasoning',
        'role':'reasoning', 'provider':'openai-compatible',
        'model':'gemma4:12b-it-qat', 'base_url':'http://127.0.0.1:11434/v1',
        'api_key_env':'ALIGO_OLLAMA_API_KEY', 'max_output_tokens':8192}]}))
    gateway = ModelGateway(SimpleNamespace(models_file=cfg, request_timeout=20))
    await gateway.complete('reasoning', 'JSON blueprint', 'JSON context', json_mode=True)
    assert captured[0]['max_tokens'] == 8192
    assert captured[0]['response_format']['type'] == 'json_object'
    assert 'extra_body' not in captured[0]
    await gateway.complete_daily_detail(DAY_SYSTEM, '{}', day_number=2)
    assert captured[1]['max_tokens'] == 8192
    assert captured[1]['response_format']['type'] == 'json_schema'
    assert captured[1]['extra_body']['reasoning_effort'] == 'none'
    assert captured[1]['response_format']['json_schema']['schema']['properties']['days']['items']['properties']['day']['enum'] == [2]
    await gateway.complete_daily_detail(DAY_COMPACT_SYSTEM, '{}', day_number=2, compact=True)
    assert captured[2]['max_tokens'] == 4096
    assert gateway.last_completion['max_output_tokens'] == 4096


@pytest.mark.asyncio
async def test_bounded_day_cannot_raise_provider_configured_output_limit(monkeypatch, tmp_path):
    captured = []

    class StubCompletions:
        async def create(self, **kwargs):
            captured.append(kwargs)
            return SimpleNamespace(choices=[SimpleNamespace(
                finish_reason='stop', message=SimpleNamespace(content='{"days":[]}'))])

    class StubAsyncOpenAI:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=StubCompletions())

    monkeypatch.setitem(sys.modules, 'openai', types.SimpleNamespace(AsyncOpenAI=StubAsyncOpenAI))
    monkeypatch.setenv('ALIGO_OLLAMA_API_KEY', 'ollama')
    cfg = tmp_path / 'models.json'
    cfg.write_text(json.dumps({'profiles': [{'name':'ollama-reasoning',
        'role':'reasoning', 'provider':'openai-compatible',
        'model':'gemma4:12b-it-qat', 'base_url':'http://127.0.0.1:11434/v1',
        'api_key_env':'ALIGO_OLLAMA_API_KEY', 'max_output_tokens':2048}]}))
    gateway = ModelGateway(SimpleNamespace(models_file=cfg, request_timeout=20))
    await gateway.complete_daily_detail(DAY_SYSTEM, '{}', day_number=3)
    assert captured[0]['max_tokens'] == 2048

@pytest.mark.asyncio
async def test_older_ollama_schema_rejection_retries_json_mode_once(monkeypatch, tmp_path):
    seen = []

    class FormatNotSupported(Exception):
        status_code = 400

    class StubCompletions:
        async def create(self, **kwargs):
            seen.append(kwargs)
            if kwargs['response_format']['type'] == 'json_schema':
                raise FormatNotSupported('response_format json_schema unsupported')
            return SimpleNamespace(choices=[SimpleNamespace(
                finish_reason='stop', message=SimpleNamespace(content=json.dumps(_single_day(2))))])

    class StubOpenAI:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=StubCompletions())

    monkeypatch.setitem(sys.modules, 'openai', types.SimpleNamespace(AsyncOpenAI=StubOpenAI))
    monkeypatch.setenv('ALIGO_OLLAMA_API_KEY', 'ollama')
    cfg = tmp_path / 'models.json'
    cfg.write_text(json.dumps({'profiles': [{'name':'ollama-reasoning',
        'role':'reasoning', 'provider':'openai-compatible',
        'model':'gemma4:12b-it-qat', 'base_url':'http://127.0.0.1:11434/v1',
        'api_key_env':'ALIGO_OLLAMA_API_KEY', 'max_output_tokens':8192}]}))
    gateway = ModelGateway(SimpleNamespace(models_file=cfg, request_timeout=20))
    assert (await gateway.complete_daily_detail(DAY_SYSTEM, '{}', day_number=2)) == _single_day(2)
    assert [r['response_format']['type'] for r in seen] == ['json_schema', 'json_object']
    assert all(r['max_tokens'] == 8192 for r in seen)
    assert gateway.last_completion['schema_fallback'] is True
