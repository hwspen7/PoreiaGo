"""Offline contract and regression tests for the unified V3 runtime."""
from dataclasses import replace
from pathlib import Path
import asyncio
import json
import pytest
from httpx import AsyncClient,ASGITransport
from app.api import create_app
from core.config import settings
from core.contracts import Evidence
from storage.memory import MemoryStore
from services.providers import ProviderResult,ProviderUnavailable
from services.registry import SkillRegistry
from core.runtime import Runtime,extract_fields
from intelligence.models import ModelGateway

ROOT=Path(__file__).resolve().parents[2]

@pytest.fixture
def app(tmp_path):
    application = create_app(database_path=tmp_path/'test.sqlite3')
    # Contract tests must never depend on live network settings in .env.
    # Tests requiring provider data inject deterministic mock adapters.
    application.state.providers.settings = replace(settings, allow_online=False)
    application.state.retrieval.settings = replace(settings, retrieval_mode='lexical')
    return application

@pytest.mark.asyncio
async def test_health_and_catalog(app):
    async with AsyncClient(transport=ASGITransport(app=app),base_url='http://testclient') as c:
        h=(await c.get('/api/health')).json()
        assert h['version']=='3.6.3' and h['skill_count']==18
        assert h['implemented_skills']==18 and h['planned_skills']==0
        skills=(await c.get('/api/skills')).json()['skills']
        assert len(skills)==len(set(x['id'] for x in skills))==18
        assert len(app.openapi()['paths'])>=15

@pytest.mark.asyncio
async def test_budgets_and_context(app):
    async with AsyncClient(transport=ASGITransport(app=app),base_url='http://testclient') as c:
        request={'user_id':'one','session_id':'holiday','message':'Plan a 3-day trip from Shanghai to Hangzhou, departing October 23, 2026, with a budget of 1,500 CNY.'}
        r=(await c.post('/api/chat',json=request)).json()
        assert r['fields']['departure']=='2026-10-23'
        assert r['fields']['budget']==1500.0
        assert r['fields']['currency']=='CNY'
        budget=next(x for x in r['results'] if x['name']=='trip-budget-allocation')
        assert budget['status']=='ok' and sum(budget['data']['allocation'].values())==1500
        second=(await c.post('/api/chat',json={**request,'message':'Please update the budget to 2,000 CNY.'})).json()
        assert second['fields']['destination']=='Hangzhou'
        assert second['fields']['days']==3
        assert second['fields']['budget']==2000
        assert second['fields']['currency']=='CNY'

@pytest.mark.asyncio
async def test_coastal_run_does_not_invent_route(app):
    async with AsyncClient(transport=ASGITransport(app=app),base_url='http://testclient') as c:
        response=(await c.post('/api/chat',json={'message':'Plan a scenic coastal running trip in Qingdao and recommend clothes for the weather.'})).json()
        assert response['fields']['destination']=='Qingdao'
        entries={r['name']:r for r in response['results']}
        assert 'coastal-running-route' not in entries
        assert entries['hourly-weather-forecast']['status']=='unavailable'
        assert not any('km' in r['text'] and 'Verified walking' in r['text'] for r in entries.values())

@pytest.mark.asyncio
async def test_mock_weather_and_clothing_are_sourced(app,monkeypatch):
    async def provider(capability,**kwargs):
        if capability=='geocode':return ProviderResult({'results':[{'latitude':36.06,'longitude':120.38}]},[Evidence(provider='test-geocoder')])
        if capability=='weather':return ProviderResult({'timezone':'Asia/Shanghai','hourly':{'time':['2026-10-20T08:00'],'apparent_temperature':[7],'temperature_2m':[8],'wind_speed_10m':[33],'precipitation_probability':[60]}},[Evidence(provider='test-weather')])
        raise ProviderUnavailable('Not configured')
    monkeypatch.setattr(app.state.providers,'request',provider)
    async with AsyncClient(transport=ASGITransport(app=app),base_url='http://testclient') as c:
        response=(await c.post('/api/chat',json={'message':'What is the weather in Qingdao and what should I wear?'})).json()
        results={r['name']:r for r in response['results']}
        assert results['hourly-weather-forecast']['status']=='ok'
        assert 'weather-aware-clothing' not in results
        assert results['hourly-weather-forecast']['sources'][1]['provider']=='test-weather'

@pytest.mark.asyncio
async def test_flights_need_provider_not_fake_prices(app):
    async with AsyncClient(transport=ASGITransport(app=app),base_url='http://testclient') as c:
        output=(await c.post('/api/chat',json={'message':'Find flights from SFO to AUS on 2026-11-10.'})).json()
        assert not any(r['name']=='flight-offer-search' for r in output['results'])

@pytest.mark.asyncio
async def test_preferences_require_consent_and_isolate_users(app):
    async with AsyncClient(transport=ASGITransport(app=app),base_url='http://testclient') as c:
        data={'user_id':'alice','category':'pace','value':'relaxed','consent':False}
        bad=await c.post('/api/preferences',json=data)
        assert bad.status_code==403
        success=await c.post('/api/preferences',json={**data,'consent':True})
        assert success.status_code==200
        assert 'pace' in (await c.get('/api/preferences?user_id=alice')).json()['preferences']
        assert (await c.get('/api/preferences?user_id=bob')).json()['preferences']=={}
        await c.delete('/api/users/alice/data')
        assert (await c.get('/api/preferences?user_id=alice')).json()['preferences']=={}

@pytest.mark.asyncio
async def test_memory_persists_after_recreating_store(tmp_path):
    path=tmp_path/'memory.sqlite3'
    first=MemoryStore(path);first.add_message('a','session','user','I prefer long walks.')
    second=MemoryStore(path)
    assert second.messages('a','session')[-1]['content']=='I prefer long walks.'
    assert second.messages('b','session')==[]

@pytest.mark.asyncio
async def test_stream_completion(app):
    async with AsyncClient(transport=ASGITransport(app=app),base_url='http://testclient') as c:
        async with c.stream('POST','/api/chat/stream',json={'message':'Allocate a travel budget of 800 USD for 2 days.'}) as r:
            data=''.join([chunk async for chunk in r.aiter_text()])
            assert r.status_code==200
            assert 'event: plan' in data and 'event: skill' in data and 'event: done' in data

@pytest.mark.asyncio
async def test_planned_skill_never_executes(app):
    async with AsyncClient(transport=ASGITransport(app=app),base_url='http://testclient') as c:
        result=(await c.post('/api/skills/water-sport-discovery/run',json={'query':'water sports'})).json()
        assert result.get('detail')=='Unknown skill.'

@pytest.mark.asyncio
async def test_optional_model_roles_are_independent(monkeypatch):
    monkeypatch.delenv('POREIAGO_EMBEDDING_MODEL', raising=False)
    monkeypatch.delenv('POREIAGO_RERANKER_MODEL', raising=False)
    models=ModelGateway(settings)
    roles={x['role'] for x in models.catalog()}
    assert {'router','reasoning','synthesis','embedding','reranking'}<=roles
    assert models.embedding_model_id()=='intfloat/multilingual-e5-large-instruct'

@pytest.mark.asyncio
async def test_knowledge_is_scoped(app):
    async with AsyncClient(transport=ASGITransport(app=app),base_url='http://testclient') as c:
        result=await c.post('/api/knowledge/documents',json={'user_id':'a','title':'Travel policy','content':'Hotel reimbursement is limited to 120 USD per night.'})
        assert result.status_code==200
        one=(await c.get('/api/knowledge/search?user_id=a&query=hotel+reimbursement')).json()
        two=(await c.get('/api/knowledge/search?user_id=b&query=hotel+reimbursement')).json()
        assert one['hits'] and not two['hits']

@pytest.mark.asyncio
async def test_expenses_and_conflict_detection(app):
    async with AsyncClient(transport=ASGITransport(app=app),base_url='http://testclient') as c:
        record=await c.post('/api/skills/expense-tracking/run',json={'arguments':{'action':'add','amount':28,'currency':'USD','category':'food','consent':True}})
        assert record.status_code==404 and record.json()['detail']=='Unknown skill.' 
        events=[{'name':'A','start':'2026-11-10T10:00','end':'2026-11-10T11:00'},{'name':'B','start':'2026-11-10T10:30','end':'2026-11-10T11:30'}]
        found=(await c.post('/api/skills/schedule-conflict-validation/run',json={'arguments':{'events':events}})).json()
        assert found['data']['conflicts'][0]['previous']=='A'

@pytest.mark.asyncio
async def test_provider_adapter_can_be_replaced(app):
    async def custom(**kwargs):return ProviderResult({'results':[{'name':'Custom location'}]},[Evidence(provider='custom')])
    app.state.providers.register('geocode','custom',custom,replace=True)
    assert app.state.providers.adapters['geocode'][0]=='custom'


def test_english_only_tracked_sources():
    include=[Path(name) for name in ('app','core','intelligence','planning','services','storage','quality','models','evaluation','maintenance','startup','web/src','plugins','tests','config','docs')]
    for folder in include:
        for path in (Path(__file__).resolve().parents[2]/folder).rglob('*'):
            if path.suffix not in {'.py','.ts','.tsx','.css','.json','.md'}:continue
            content=path.read_text(encoding='utf-8')
            assert not any(chr(0x4e00)<=ch<=chr(0x9fff) for ch in content),str(path)
