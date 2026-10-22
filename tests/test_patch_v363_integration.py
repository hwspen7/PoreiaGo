"""Regression tests for the eighteen-Skill integration and reliability patch."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest
from httpx import ASGITransport, AsyncClient

from aligo.api import create_app
from aligo.models import ModelGateway
from aligo.providers import ProviderGateway, ProviderResult, ProviderUnavailable
from aligo.contracts import Evidence
from aligo.registry import SkillRegistry
from aligo.runtime import Runtime
from aligo.travel_intelligence import TravelSkillOrchestrator
from scripts.benchmark_skill_modes import summarize

ROOT = Path(__file__).resolve().parents[1]


def controller():
    return TravelSkillOrchestrator(SkillRegistry(ROOT / 'plugins' / 'skills'), None)


def candidates():
    return [
        {'name': 'City Museum', 'area': 'Old Quarter',
         'opening_hours': '09:00-17:00', 'reservation_required': True,
         'ticket_price': 10, 'currency': 'USD',
         'season_months': [10], 'accessibility': {'step_free': False}},
        {'name': 'Local Cafe', 'area': 'Old Quarter', 'type': 'restaurant',
         'cuisine': 'regional'},
    ]


@pytest.mark.asyncio
async def test_mapped_provider_candidates_reach_daily_model_context():
    orchestrator = controller()
    day = await orchestrator.day_context({'city': 'Example City', 'places': candidates()},
        {'day': 1, 'city': 'Example City', 'date': '2026-10-20',
         'theme': 'food and culture'},
        request='Local food and historic culture',
        fields={'accessibility_requirements': ['step_free']})
    assert 'professional_evidence' in day
    assert {'opening-hours-intelligence', 'ticket-entry-planning',
            'accessibility-suitability', 'culinary-specialty-discovery'} <= set(day['professional_evidence'])
    assert any(row['skill'] == 'accessibility-suitability' for row in orchestrator.trace)


@pytest.mark.asyncio
async def test_accessibility_is_not_dropped_by_feature_order(monkeypatch):
    monkeypatch.setenv('ALIGO_PROFESSIONAL_SKILL_BUDGET', '5')
    orchestrator = controller()
    response = await orchestrator.professional_day_context(candidates(),
        {'day': 1, 'city': 'Example City', 'date': '2026-10-20',
         'theme': 'food festival and local crafts'},
        request='Food and local events', fields={
            'accessibility_requirements': ['step_free'],
            'event_candidates': [{'name': 'Event A', 'start_date': '2026-10-20'}],
            'month': 10, 'cuisine': 'regional'})
    assert len(response) <= 5
    assert 'accessibility-suitability' in response
    assert any(row['skill'] == 'professional-skill-routing' for row in orchestrator.trace)


@pytest.mark.asyncio
async def test_explicit_budget_can_expand_results(monkeypatch):
    monkeypatch.setenv('ALIGO_PROFESSIONAL_SKILL_BUDGET', '5')
    orchestrator = controller()
    expanded = await orchestrator.professional_day_context(candidates(),
        {'day': 1, 'city': 'Example City', 'date': '2026-10-20',
         'theme': 'food and local events'},
        request='Food and event', fields={
            'accessibility_requirements': ['step_free'], 'month': 10,
            'event_candidates': [{'name': 'Event A', 'start_date': '2026-10-20'}],
            'cuisine': 'regional', 'professional_skill_budget': 9})
    assert len(expanded) >= 6


@pytest.mark.asyncio
async def test_shared_profile_has_independent_request_deadlines(tmp_path, monkeypatch):
    profiles = [
        {'name': 'r', 'role': 'router', 'model': 'model', 'provider': 'openai-compatible',
         'base_url': 'http://127.0.0.1:11434/v1', 'api_key_env': 'PATCH_KEY'},
        {'name': 'shared', 'role': 'reasoning', 'model': 'model', 'provider': 'openai-compatible',
         'base_url': 'http://127.0.0.1:11434/v1', 'api_key_env': 'PATCH_KEY'},
        {'name': 's', 'role': 'synthesis', 'model': 'model', 'provider': 'openai-compatible',
         'base_url': 'http://127.0.0.1:11434/v1', 'api_key_env': 'PATCH_KEY'},
    ]
    models_file = tmp_path / 'models.json'
    models_file.write_text(json.dumps({'profiles': profiles}), encoding='utf-8')
    stages = {k: ('r' if k == 'router' else 's' if k == 'synthesis' else 'shared')
              for k in ('router', 'reasoning', 'synthesis', 'blueprint', 'daily', 'repair')}
    stages['daily'] = {'profile': 'shared', 'request_timeout_seconds': 31}
    stages['repair'] = {'profile': 'shared', 'request_timeout_seconds': 999}
    preset = tmp_path / 'preset.json'
    preset.write_text(json.dumps({'version': 1, 'presets': {'trial': {'stages': stages}}}),
                      encoding='utf-8')
    monkeypatch.setenv('PATCH_KEY', 'test-local-key')
    calls = []

    class FakeCompletions:
        async def create(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(choices=[SimpleNamespace(finish_reason='stop',
                                        message=SimpleNamespace(content='{"done":true}'))],
                                   usage=None)

    fake_client = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions()))
    monkeypatch.setitem(sys.modules, 'openai', SimpleNamespace(AsyncOpenAI=lambda **kw: fake_client))
    gateway = ModelGateway(SimpleNamespace(models_file=models_file, request_timeout=45),
                           preset='trial', presets_path=preset)
    await gateway.complete('reasoning', 'system', 'request', json_mode=True, stage='daily')
    await gateway.complete('reasoning', 'system', 'request', json_mode=True, stage='repair')
    assert [call['timeout'] for call in calls] == [31., 999.]
    assert len(gateway._clients) == 1


@pytest.mark.asyncio
async def test_provider_cache_deduplicates_but_offline_stays_unavailable(tmp_path):
    settings = SimpleNamespace(allow_online=True, request_timeout=1, ors_token='',
                               duffel_token='', provider_file=tmp_path / 'none.json')
    gateway = ProviderGateway(settings)
    counts = {'geocode': 0, 'weather': 0}

    async def geocode(**kwargs):
        counts['geocode'] += 1
        await asyncio.sleep(0.02)
        return ProviderResult({'results': [{'name': 'Example'}]},
                              [Evidence(provider='mock-geocode')])

    async def weather(**kwargs):
        counts['weather'] += 1
        return ProviderResult({'hourly': {}}, [Evidence(provider='mock-weather')])

    gateway.register('geocode', 'stub', geocode, replace=True)
    gateway.register('weather', 'stub', weather, replace=True)
    a, b = await asyncio.gather(gateway.request('geocode', place='Example'),
                                gateway.request('geocode', place='Example'))
    a.data['results'][0]['name'] = 'Mutated'
    c = await gateway.request('geocode', place='Example')
    assert b.data['results'][0]['name'] == c.data['results'][0]['name'] == 'Example'
    assert counts['geocode'] == 1
    assert gateway.cache_hits == 2
    await gateway.request('weather', latitude=0, longitude=0)
    await gateway.request('weather', latitude=0, longitude=0)
    assert counts['weather'] == 2
    settings.allow_online = False
    with pytest.raises(ProviderUnavailable):
        await gateway.request('geocode', place='Example')


@pytest.mark.asyncio
async def test_concurrent_same_session_serializes_across_runtime_instances():
    store = SimpleNamespace()
    left = Runtime(store, None, None, None, SimpleNamespace(memory_window=40), retrieval=object())
    right = Runtime(store, None, None, None, SimpleNamespace(memory_window=40), retrieval=object())
    active = 0
    maximum = 0

    async def run(*args, **kwargs):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0.02)
        active -= 1
        return 'ok'

    left._execute_unlocked = run
    right._execute_unlocked = run
    assert await asyncio.gather(left.execute('u', 'trip', 'a'),
                                right.execute('u', 'trip', 'b')) == ['ok', 'ok']
    assert maximum == 1
    assert store._aligo_session_gates == {}


@pytest.mark.asyncio
async def test_diagnostics_are_aggregated_without_travel_text(tmp_path):
    app = create_app(database_path=tmp_path / 'stats.sqlite3')
    try:
        await app.state.registry.run('neighborhood-intelligence',
            SimpleNamespace(providers=None, fields={}),
            {'candidates': [{'name': 'Secret Location', 'area': 'West'}]})
        async with AsyncClient(transport=ASGITransport(app=app),
                               base_url='http://testclient') as client:
            data = (await client.get('/api/diagnostics/skills')).json()
            assert data['skills']['neighborhood-intelligence']['sample_size'] == 1
            assert data['skills']['neighborhood-intelligence']['p95_ms'] >= 0
            assert 'Secret Location' not in json.dumps(data)
    finally:
        app.state.store.close()


def test_benchmark_summary_handles_missing_token_usage():
    report = {'results': [{'status': 'PASS', 'elapsed_seconds': 2,
                           'role_calls': [{'usage': {'total_tokens': 50}}]},
                          {'status': 'FAIL', 'elapsed_seconds': 4,
                           'role_calls': [{'usage': None}]}]}
    output = summarize(report)
    assert output['cases'] == 2 and output['passed'] == 1
    assert output['observed_model_tokens'] == 50
    assert output['average_seconds'] == 3
