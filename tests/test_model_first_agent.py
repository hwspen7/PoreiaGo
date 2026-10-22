"""Regression tests for model-first decisions, tool grounding and multilingual replies."""
from dataclasses import replace
import json
from pathlib import Path
import pytest

from aligo.config import settings
from aligo.contracts import SkillOutput
from aligo.memory import MemoryStore
from aligo.models import ModelGateway
from aligo.model_agent import model_first_plan, PlanNotUsable
from aligo.registry import SkillRegistry
from aligo.runtime import Runtime

ROOT = Path(__file__).resolve().parents[1]


class ModelStub:
    def __init__(self, plan, synthesis='Grounded answer.'):
        self.plan = plan
        self.synthesis = synthesis
        self.calls = []

    def configured(self, role):
        return role in ('router', 'synthesis')

    async def complete(self, role, system, prompt, json_mode=False):
        self.calls.append((role, system, prompt, json_mode))
        return self.plan if role == 'router' else self.synthesis


async def make_runtime(tmp_path, fake):
    store = MemoryStore(tmp_path / 'test.sqlite3')
    registry = SkillRegistry(ROOT / 'plugins/skills')
    runtime = Runtime(store, fake, object(), registry, replace(settings, agent_mode='model-first'), retrieval=object())
    return runtime, store, registry


@pytest.mark.asyncio
async def test_direct_answer_does_not_call_skills(tmp_path):
    fake = ModelStub({'mode':'direct', 'answer':'Enjoy scenic city walks and local cafes on a relaxing weekend.', 'goals':['slow travel'], 'skills':[]})
    runtime, store, registry = await make_runtime(tmp_path, fake)
    async def never_call(*args):
        raise AssertionError('No Skills should run for general advice')
    registry.run = never_call
    try:
        result = await runtime.execute('user', 'trip', 'Help me plan a relaxing weekend.')
        assert result['answer'].startswith('Enjoy scenic')
        assert result['planner_mode'] == 'model_direct'
        assert not result['results']
        assert result['plan']['skills'] == []
        assert [role for role, _, _, _ in fake.calls] == ['router']
        assert store.messages('user', 'trip')[-1]['role'] == 'assistant'
    finally:
        store.close()


@pytest.mark.asyncio
async def test_model_single_tool_always_synthesizes(tmp_path):
    fake = ModelStub({'mode':'tools','answer':'','goals':['local attractions'],
                      'fields':{'destination':'Lisbon'},
                      'skills':[{'name':'place-discovery','arguments':{}}]},
                     synthesis='Lisbon has several places worth considering; verify opening hours.')
    runtime, store, registry = await make_runtime(tmp_path, fake)
    calls = []
    async def run(name, ctx, args):
        calls.append((name,args))
        return SkillOutput(name=name,text='Verified locations',data={'results':[{'name':'Location A'}]})
    registry.run = run
    try:
        result = await runtime.execute('user','trip','Find places to explore in Lisbon.')
        assert result['planner_mode'] == 'model_tools'
        assert calls[0][0] == 'place-discovery'
        assert result['answer'].startswith('Lisbon has')
        assert [role for role, _, _, _ in fake.calls] == ['router','synthesis']
        assert 'Location A' in fake.calls[1][2]
    finally:
        store.close()


@pytest.mark.asyncio
async def test_clarification_does_not_call_synthesis(tmp_path):
    fake=ModelStub({'mode':'clarify','answer':'¿En qué mes quieres viajar?','skills':[]})
    runtime,store,_=await make_runtime(tmp_path,fake)
    try:
        result=await runtime.execute('user','trip','Quiero viajar cuando haya buen tiempo.')
        assert result['planner_mode']=='model_clarification'
        assert result['answer'].startswith('¿')
        assert len(fake.calls)==1
    finally:store.close()


@pytest.mark.asyncio
async def test_invalid_model_selection_cannot_execute_mutation(tmp_path):
    fake=ModelStub({'mode':'tools','skills':[{'name':'expense-tracking'},{'name':'unknown-command'}]})
    _,store,registry=await make_runtime(tmp_path,fake)
    try:
        with pytest.raises(PlanNotUsable):
            await model_first_plan('Spend money',[],{}, {},fake,registry,8)
    finally:store.close()


@pytest.mark.asyncio
async def test_preferences_and_history_reach_model(tmp_path):
    fake=ModelStub({'mode':'direct','answer':'Continue the earlier cultural itinerary.'})
    runtime,store,_=await make_runtime(tmp_path,fake)
    store.update_preference('user','travel_style','museums',consent=True)
    store.add_message('user','trip','user','Please recommend art museums.')
    store.add_message('user','trip','assistant','Certainly.')
    try:
        await runtime.execute('user','trip','Continue from the previous suggestion.')
        payload=json.loads(fake.calls[0][2])
        assert payload['saved_preferences']['travel_style']['value']=='museums'
        assert payload['history'][-1]['content']=='Certainly.'
    finally:store.close()


def test_groq_model_profiles_and_key_is_never_exposed(monkeypatch):
    monkeypatch.setenv('GROQ_API_KEY','secret_example_do_not_print')
    monkeypatch.setenv('ALIGO_GROQ_MODEL','qwen/qwen3.8-27b')
    models=ModelGateway(settings)
    published=models.catalog()
    assert any(p['name']=='groq-router' and p['configured'] and p['model']=='qwen/qwen3.8-27b' for p in published)
    assert 'secret_example_do_not_print' not in json.dumps(published)


@pytest.mark.asyncio
async def test_unsupported_output_rejected(tmp_path):
    fake=ModelStub({'mode':'direct','answer':''})
    _,store,registry=await make_runtime(tmp_path,fake)
    try:
        with pytest.raises(PlanNotUsable):
            await model_first_plan('Hello',[],{}, {},fake,registry,6)
    finally:store.close()

@pytest.mark.asyncio
async def test_api_chat_returns_model_generated_answer(tmp_path):
    from httpx import ASGITransport, AsyncClient
    from aligo.api import create_app
    app = create_app(database_path=tmp_path / 'chat.sqlite3')
    app.state.runtime.models = ModelStub({'mode':'direct', 'answer':'A relaxed two-day outline.', 'skills':[]})
    app.state.runtime.settings = replace(settings, agent_mode='model-first')
    try:
        async with AsyncClient(transport=ASGITransport(app=app),base_url='http://testclient') as client:
            response = await client.post('/api/chat',json={'message':'Suggest a relaxed weekend.'})
        assert response.status_code==200
        assert response.json()['answer']=='A relaxed two-day outline.'
        assert response.json()['planner_mode']=='model_direct'
    finally:
        app.state.store.close()


@pytest.mark.asyncio
async def test_model_outage_does_not_retry_the_same_router(tmp_path):
    from aligo.models import ModelUnavailable
    class BrokenModel:
        def __init__(self):self.calls=[]
        def configured(self,role):return role=='router'
        async def complete(self,role,system,prompt,json_mode=False):
            self.calls.append(role)
            raise ModelUnavailable('HTTP 429')
    models=BrokenModel()
    runtime,store,_=await make_runtime(tmp_path,models)
    try:
        result=await runtime.execute('user','trip','Find scenic viewpoints in Lisbon')
        assert result['planner_mode']=='catalog_fallback'
        assert models.calls==['router']
        assert any('Cloud model planning failed' in x for x in result['planner_limitations'])
    finally:store.close()
