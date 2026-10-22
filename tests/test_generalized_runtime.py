"""V3.1 behavior: extensible discovery, dependency graphs, and bounded execution."""
from __future__ import annotations
import asyncio
import json
import time
from dataclasses import replace
from pathlib import Path
import pytest
from httpx import ASGITransport, AsyncClient
from aligo.api import create_app
from aligo.config import settings
from aligo.contracts import SkillOutput, Evidence
from aligo.intelligence import CatalogMatcher, understand_intent
from aligo.planning import PlanCompiler
from aligo.providers import ProviderGateway, ProviderResult, ProviderUnavailable
from aligo.registry import SkillRegistry
from aligo.evaluation import evaluate_catalog, load_suite

ROOT = Path(__file__).resolve().parents[1]


class OfflineModelStub:
    """Keep scheduler regressions independent of external model credentials."""

    def configured(self, role):
        return False

    async def complete(self, *args, **kwargs):
        raise AssertionError('A live model request is not allowed in this unit test.')


def disable_live_models(app):
    """Use the deterministic catalog path for scheduler and API contract tests."""
    app.state.runtime.models = OfflineModelStub()
    app.state.models = app.state.runtime.models


def test_broad_catalog_coverage():
    registry = SkillRegistry(ROOT/'plugins/skills')
    result = evaluate_catalog(registry, load_suite(ROOT/'evaluations/catalog_v1.json'))
    assert result['cases'] >= 9
    assert result['passed'] == result['cases'], result


def test_dependency_closure_and_layering():
    registry = SkillRegistry(ROOT/'plugins/skills')
    # Independently installed Skills can declare dependencies without router edits.
    registry.get('place-detail-verification').depends_on = ['place-discovery']
    plan = PlanCompiler(registry).compile([{'name':'place-detail-verification','arguments':{}}])
    assert plan.as_dict()['layers'] == [['place-discovery'], ['place-detail-verification']]
    assert not plan.rejected


def test_planner_rejects_unknown_and_side_effects():
    registry = SkillRegistry(ROOT/'plugins/skills')
    plan = PlanCompiler(registry).compile([{'name':'water-sport-discovery'}, {'name':'expense-tracking'}, {'name':'place-discovery'}])
    assert plan.names == ['place-discovery']
    assert len(plan.rejected) == 2


def test_planner_dependency_limit_no_orphans():
    registry = SkillRegistry(ROOT/'plugins/skills')
    registry.get('place-detail-verification').depends_on = ['place-discovery']
    plan = PlanCompiler(registry, maximum=1).compile([{'name':'place-detail-verification'}])
    assert plan.names == []
    assert len(plan.rejected) == 1


def test_manifest_extension_selected_without_router_edits(tmp_path):
    root = tmp_path/'skills'
    path = root/'new-custom-experience'
    path.mkdir(parents=True)
    manifest = {'id':'new-custom-experience','description':'Custom local experience',
                'domain':'Experimental','wave':'P3','release_state':'implemented',
                'entrypoint':'aligo.handlers:relaxed_day',
                'keywords':['novel ocean experience'],'mutates_state':False}
    (path/'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
    registry = SkillRegistry(root)
    matches = CatalogMatcher(registry).rank('Suggest a novel ocean experience', {})
    assert matches[0]['name'] == 'new-custom-experience'


@pytest.mark.asyncio
async def test_model_router_allowlist_and_field_override(tmp_path):
    app = create_app(database_path=tmp_path/'model.sqlite3')
    class FakeModels:
        def configured(self, role):return role == 'router'
        async def complete(self, role, system, prompt, json_mode=False):
            return {'fields':{'destination':'Rome'},'goals':['museum exploration'],
                    'skills':[{'name':'place-discovery','arguments':{}},
                              {'name':'arbitrary-command','arguments':{}},
                              {'name':'expense-tracking','arguments':{}}]}
    fake = FakeModels()
    frame = await understand_intent('Find museums in Rome', [], {'destination':'Rome'}, fake, app.state.registry, 8)
    assert frame.mode == 'model_router'
    assert [s['name'] for s in frame.skill_calls] == ['place-discovery']
    assert frame.fields['destination'] == 'Rome'


@pytest.mark.asyncio
async def test_parallel_siblings_then_dependency(tmp_path, monkeypatch):
    app = create_app(database_path=tmp_path/'parallel.sqlite3')
    disable_live_models(app)
    app.state.runtime.settings = replace(settings, max_parallel_skills=4, execution_timeout=8)
    app.state.registry.get('place-detail-verification').depends_on = ['place-discovery']
    completed = {}
    async def fake_run(name, ctx, args):
        if name in ('hourly-weather-forecast','place-discovery'):
            await asyncio.sleep(0.08)
        if name == 'place-detail-verification':
            assert 'place-discovery' in ctx.shared
        completed[name] = time.perf_counter()
        return SkillOutput(name=name, text=name)
    monkeypatch.setattr(app.state.registry,'run',fake_run)
    async def select(query,history,fields):
        from aligo.intelligence import IntentFrame
        return IntentFrame(fields=fields,skill_calls=[{'name':'place-detail-verification'}, {'name':'place-discovery'}])
    monkeypatch.setattr('aligo.runtime.understand_intent',lambda q,h,f,m,r,n: select(q,h,f))
    out = await app.state.runtime.execute('test','t','Find places and recommend clothes')
    assert out['plan']['layers'] == [['place-discovery'],['place-detail-verification']]
    assert completed['place-detail-verification'] >= completed['place-discovery']
    assert len(out['execution_trace']) == 2


@pytest.mark.asyncio
async def test_model_first_tool_plan_preserves_dependency_layers(tmp_path, monkeypatch):
    """A model-selected tool plan must still respect the dependency scheduler."""
    app = create_app(database_path=tmp_path/'model_dag.sqlite3')
    app.state.runtime.settings = replace(settings, agent_mode='model-first', max_parallel_skills=4)
    app.state.registry.get('place-detail-verification').depends_on = ['place-discovery']

    class RouterOnlyModels:
        def configured(self, role):
            return role == 'router'

        async def complete(self, *args, **kwargs):
            raise AssertionError('Model planning must be stubbed for this test.')

    app.state.runtime.models = RouterOnlyModels()
    app.state.models = app.state.runtime.models

    from aligo.intelligence import IntentFrame

    async def fake_model_plan(*args, **kwargs):
        frame = IntentFrame(
            fields={},
            skill_calls=[{'name': 'place-detail-verification'}, {'name': 'place-discovery'}],
            mode='model_tools',
        )
        return frame, None

    async def fake_run(name, ctx, args):
        if name == 'place-detail-verification':
            assert 'place-discovery' in ctx.shared
        return SkillOutput(name=name, text=name)

    monkeypatch.setattr('aligo.runtime.model_first_plan', fake_model_plan)
    monkeypatch.setattr(app.state.registry, 'run', fake_run)
    out = await app.state.runtime.execute('test', 'model-first-dag', 'Find places and recommend clothes')

    assert out['planner_mode'] == 'model_tools'
    assert out['plan']['layers'] == [
        ['place-discovery'], ['place-detail-verification']
    ]
    assert {item['name'] for item in out['execution_trace']} == {
        'place-discovery', 'place-detail-verification'
    }


@pytest.mark.asyncio
async def test_overall_deadline_is_bounded(tmp_path, monkeypatch):
    app = create_app(database_path=tmp_path/'deadline.sqlite3')
    disable_live_models(app)
    app.state.runtime.settings = replace(settings, execution_timeout=0.04)
    async def slow(name, ctx, args):
        await asyncio.sleep(0.3)
        return SkillOutput(name=name,text='done')
    monkeypatch.setattr(app.state.registry,'run',slow)
    out = await app.state.runtime.execute('a','b','Find attractions in Tallinn')
    assert any(item.get('error_code') == 'EXECUTION_DEADLINE' for item in out['results'])


@pytest.mark.asyncio
async def test_provider_fallback_with_real_data_contract(tmp_path):
    providers = ProviderGateway(replace(settings, allow_online=True, provider_file=tmp_path/'missing.json', request_timeout=1))
    async def broken(**kw):
        raise ProviderUnavailable('upstream offline')
    async def replacement(**kw):
        return ProviderResult({'results':[{'name':'Verified provider data'}]}, [Evidence(provider='mock-backup')])
    providers.register('geocode','broken',broken,replace=True)
    providers.register_fallback('geocode','backup',replacement)
    result = await providers.request('geocode',place='A city')
    assert result.evidence[0].provider == 'mock-backup'
    assert next(row for row in providers.catalog() if row['capability'] == 'geocode')['fallback_providers'] == ['backup']


def test_model_profile_runtime_extension(tmp_path):
    app=create_app(database_path=tmp_path/'profile.sqlite3')
    models=app.state.models
    profile={'name':'alternate-role','role':'vision','provider':'openai-compatible','model':'example-model','base_url':'https://example.invalid/v1','api_key_env':'NONEXISTENT_KEY'}
    models.register_profile(profile)
    assert any(p['role']=='vision' for p in models.catalog())
    with pytest.raises(ValueError): models.register_profile(profile)


@pytest.mark.asyncio
async def test_api_exposes_execution_plan_and_trace(tmp_path):
    app=create_app(database_path=tmp_path/'api.sqlite3')
    disable_live_models(app)
    app.state.providers.settings=replace(settings, allow_online=False)
    async with AsyncClient(transport=ASGITransport(app=app),base_url='http://testclient') as client:
        r=await client.post('/api/chat',json={'message':'Find museums in Kyoto'})
    assert r.status_code==200
    data=r.json()
    assert 'place-discovery' in data['plan']['skills']
    assert data['execution_trace'][0]['name']=='place-discovery'
    assert data['results'][0]['status']=='unavailable'
