"""Regression coverage for independent daily output budgets and local JSON mode."""
from __future__ import annotations

import asyncio
import json
import sys
import types
from types import SimpleNamespace

import pytest

from aligo.models import (
    DAILY_DETAIL_MAX_OUTPUT_TOKENS,
    DAILY_DETAIL_RETRY_OUTPUT_TOKENS,
    ModelGateway,
    ModelUnavailable,
    _is_loopback_ollama,
)
from scripts.evaluate_travel_quality import TimedGateway


def _gateway(tmp_path, monkeypatch, *, base_url='http://127.0.0.1:11434/v1',
             provider_limit=8192, max_completion_tokens=None,
             finish_reason='stop'):
    captured = []

    class Completions:
        async def create(self, **kwargs):
            captured.append(kwargs)
            day = kwargs['response_format'].get('json_schema', {}).get('schema', {}).get(
                'properties', {}).get('days', {}).get('items', {}).get('properties', {}).get(
                'day', {}).get('enum', [1])[0]
            return SimpleNamespace(choices=[SimpleNamespace(
                finish_reason=finish_reason,
                message=SimpleNamespace(content=json.dumps({'days': [{'day': day}]})))],
                usage=SimpleNamespace(prompt_tokens=110,
                                      completion_tokens=42, total_tokens=152))

    class OpenAI:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=Completions())

    monkeypatch.setitem(sys.modules, 'openai', types.SimpleNamespace(AsyncOpenAI=OpenAI))
    monkeypatch.setenv('ALIGO_OLLAMA_API_KEY', 'ollama')
    profile = {'name': 'local-reasoning', 'role': 'reasoning',
               'provider': 'openai-compatible', 'model': 'gemma4:12b-it-qat',
               'base_url': base_url, 'api_key_env': 'ALIGO_OLLAMA_API_KEY'}
    if provider_limit is not None:
        profile['max_output_tokens'] = provider_limit
    if max_completion_tokens is not None:
        profile['max_completion_tokens'] = max_completion_tokens
    path = tmp_path / 'models.json'
    path.write_text(json.dumps({'profiles': [profile]}), encoding='utf-8')
    return ModelGateway(SimpleNamespace(models_file=path, request_timeout=20)), captured


def test_daily_generation_budget_constants_and_config_are_consistent():
    from pathlib import Path
    assert DAILY_DETAIL_MAX_OUTPUT_TOKENS == 8192
    assert DAILY_DETAIL_RETRY_OUTPUT_TOKENS == 4096
    config = json.loads((Path(__file__).resolve().parents[1] / 'config' /
                         'models.json').read_text(encoding='utf-8'))
    profiles = {p['name']: p for p in config['profiles']}
    assert profiles['ollama-router']['max_output_tokens'] == 3072
    assert profiles['ollama-reasoning']['max_output_tokens'] == 8192
    assert profiles['ollama-synthesis']['max_output_tokens'] == 8192


@pytest.mark.asyncio
@pytest.mark.parametrize('day_number', [1, 2, 7, 11, 14, 15, 21, 30])
async def test_every_day_gets_its_own_full_budget(tmp_path, monkeypatch, day_number):
    gateway, calls = _gateway(tmp_path, monkeypatch)
    tracked = TimedGateway(gateway)
    result = await tracked.complete_daily_detail('Generate one day', '{}',
                                                  day_number=day_number)
    assert result['days'][0]['day'] == day_number
    assert len(calls) == 1
    assert calls[0]['max_tokens'] == 8192
    assert calls[0]['response_format']['type'] == 'json_schema'
    assert (calls[0]['response_format']['json_schema']['schema']
            ['properties']['days']['items']['properties']['day']['enum'] == [day_number])
    assert tracked.role_calls[0]['day'] == day_number
    assert tracked.role_calls[0]['usage']['max_output_tokens'] == 8192
    assert tracked.role_calls[0]['usage']['completion_tokens'] == 42


@pytest.mark.asyncio
async def test_previous_day_usage_cannot_reduce_next_day_limit(tmp_path, monkeypatch):
    gateway, calls = _gateway(tmp_path, monkeypatch)
    tracked = TimedGateway(gateway)
    for number in (1, 2, 3, 11):
        await tracked.complete_daily_detail('Generate one day', '{}', day_number=number)
    assert [call['max_tokens'] for call in calls] == [8192] * 4
    assert [call['day'] for call in tracked.role_calls] == [1, 2, 3, 11]


@pytest.mark.asyncio
async def test_compact_retry_is_smaller_and_does_not_lower_next_day(tmp_path, monkeypatch):
    gateway, calls = _gateway(tmp_path, monkeypatch)
    await gateway.complete_daily_detail('Repair truncated Day 3', '{}',
                                        day_number=3, compact=True)
    await gateway.complete_daily_detail('Generate Day 4', '{}', day_number=4)
    assert [call['max_tokens'] for call in calls] == [4096, 8192]


@pytest.mark.asyncio
async def test_lower_provider_cap_remains_authoritative(tmp_path, monkeypatch):
    gateway, calls = _gateway(tmp_path, monkeypatch, provider_limit=2000)
    await gateway.complete_daily_detail('one day', '{}', day_number=2)
    assert calls[0]['max_tokens'] == 2000


@pytest.mark.asyncio
async def test_provider_completion_limit_is_not_overridden(tmp_path, monkeypatch):
    gateway, calls = _gateway(tmp_path, monkeypatch, provider_limit=None,
                              max_completion_tokens=2600)
    await gateway.complete_daily_detail('one day', '{}', day_number=2)
    assert 'max_tokens' not in calls[0]
    assert calls[0]['extra_body']['max_completion_tokens'] == 2600


@pytest.mark.asyncio
async def test_truncation_metadata_keeps_requested_daily_limit(tmp_path, monkeypatch):
    gateway, calls = _gateway(tmp_path, monkeypatch, finish_reason='length')
    tracked = TimedGateway(gateway)
    with pytest.raises(ModelUnavailable, match='OUTPUT_TRUNCATED'):
        await tracked.complete_daily_detail('one day', '{}', day_number=2)
    assert calls[0]['max_tokens'] == 8192
    assert tracked.role_calls[0]['status'] == 'failed'
    assert tracked.role_calls[0]['usage']['finish_reason'] == 'length'
    assert tracked.role_calls[0]['usage']['max_output_tokens'] == 8192


@pytest.mark.parametrize('url,expected', [
    ('http://127.0.0.1:11434/v1', True),
    ('http://localhost:11434/v1', True),
    ('http://[::1]:11434/v1', True),
    ('http://127.0.0.1:11434.evil.test/v1', False),
    ('http://127.0.0.1.evil.test:11434/v1', False),
    ('http://localhost:11435/v1', False),
    ('https://api.openai.com/v1', False),
])
def test_local_schema_detection_is_narrow(url, expected):
    assert _is_loopback_ollama(url) is expected


@pytest.mark.asyncio
async def test_localhost_schema_applies_in_real_gateway(tmp_path, monkeypatch):
    gateway, calls = _gateway(tmp_path, monkeypatch,
                              base_url='http://localhost:11434/v1')
    await gateway.complete_daily_detail('one day', '{}', day_number=5)
    assert calls[0]['response_format']['type'] == 'json_schema'
    assert calls[0]['extra_body']['reasoning_effort'] == 'none'


def test_daily_timeout_defaults_and_clamps(monkeypatch):
    from aligo.config import Settings
    from aligo.segmented_planning import generate_daily_trip
    import inspect
    import os
    import subprocess
    for raw, expected in [('30', 65), ('180', 180), ('260', 260), ('500', 300)]:
        environment = {**os.environ, 'ALIGO_DAILY_GENERATION_TIMEOUT': raw}
        value = subprocess.check_output(
            [sys.executable, '-c',
             'from aligo.config import Settings; print(Settings().daily_generation_timeout)'],
            env=environment, text=True)
        assert float(value) == expected
    assert inspect.signature(generate_daily_trip).parameters['timeout'].default == 180.0


@pytest.mark.asyncio
async def test_runtime_uses_daily_timeout_not_short_router_timeout(monkeypatch):
    from dataclasses import replace
    from aligo.config import settings
    from aligo.runtime import Runtime

    seen = []
    async def fake_daily(*args, **kwargs):
        seen.append(kwargs)
        return {'destination': 'Columbus', 'title': 'Three-day city trip',
                'days': [{'day': n, 'title': f'Columbus Day {n}',
                          'activities': [{'description': 'Explore a cultural attraction'}]}
                         for n in range(1, 4)]}

    monkeypatch.setattr('aligo.runtime.generate_daily_trip', fake_daily)

    class DummyModels:
        def daily_planning_enabled(self):
            return True

    gateway = Runtime(None, DummyModels(), None, None,
                      replace(settings, intent_timeout=25, daily_generation_timeout=180),
                      retrieval=object())
    frame = SimpleNamespace(task={'operation': 'create', 'itinerary': None})
    itinerary, _, _ = await gateway._prepare_trip_update(
        'Plan a 3 day trip in Columbus.', [], None, '', frame, None, [], {},
        {'destination': 'Columbus', 'days': 3})
    assert len(itinerary['days']) == 3
    assert seen[0]['timeout'] == 180.0
