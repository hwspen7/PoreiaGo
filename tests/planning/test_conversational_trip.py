"""End-to-end stateful itinerary workflows without external API traffic."""
import json
from dataclasses import replace
from pathlib import Path

import pytest

from core.config import settings
from core.contracts import SkillOutput
from storage.memory import MemoryStore
from services.registry import SkillRegistry
from core.runtime import Runtime
from planning.trip_state import TripValidationError, validate_itinerary

ROOT = Path(__file__).resolve().parents[2]


def example_trip():
    return {
        'destination': 'Japan', 'title': 'Five days in Japan',
        'preferences': {'pace': 'relaxed', 'budget': 'moderate'},
        'days': [
            {'day': i, 'title': f'Day {i} in the Kansai region',
             'activities': [{'description': f'Visit cultural site {i}'},
                            {'description': f'Try local food {i}'}]}
            for i in range(1, 6)
        ]
    }


class SequenceModels:
    def __init__(self, plans, repairs=None, synthesis=None):
        self.plans = list(plans)
        self.repairs = list(repairs or [])
        self.synthesis = list(synthesis or [])
        self.calls = []

    def configured(self, role):
        return role in {'router', 'reasoning', 'synthesis'}

    async def complete(self, role, system, prompt, json_mode=False):
        self.calls.append((role, json.loads(prompt), json_mode))
        if role == 'router':
            return self.plans.pop(0)
        if role == 'reasoning':
            return self.repairs.pop(0)
        return self.synthesis.pop(0) if self.synthesis else '## Updated trip\n\n' + ('Enjoy Kyoto and local experiences. ' * 12)


def decision(operation='none', itinerary=None, mode='direct', answer=None, skills=None):
    return {'mode': mode, 'operation': operation, 'itinerary': itinerary,
            'answer': answer if answer is not None else 'Here is a complete and personalized trip. ' * 8,
            'skills': skills or [], 'goals': ['coherent journey']}


@pytest.fixture
def environment(tmp_path):
    store = MemoryStore(tmp_path / 'trip.sqlite3')
    registry = SkillRegistry(ROOT / 'plugins/skills')
    def make(models):
        return Runtime(store, models, object(), registry,
                       replace(settings, agent_mode='model-first'), retrieval=object())
    yield store, registry, make
    store.close()


@pytest.mark.asyncio
async def test_create_revise_and_cross_language_reference(environment):
    store, registry, make = environment
    original = example_trip()
    changed_day = {'day': 3, 'title': 'Nara and a relaxed food walk',
                   'activities': ['Nara Park in the morning', 'Explore a local food street']}
    models = SequenceModels([
        decision('create', original),
        decision('revise', {'changes': [changed_day], 'preferences': {'audience': 'young_travelers'}}),
        decision('discuss', answer='Resumen del plan actualizado en cinco días.'),
    ])
    runtime = make(models)
    first = await runtime.execute('alice', 'japan', 'Plan five days in Japan for food and culture.')
    assert first['active_itinerary']['version'] == 1
    assert first['trip_change']['action'] == 'created'
    second = await runtime.execute('alice', 'japan', 'Change day three for younger travelers, keep other days.')
    assert second['active_itinerary']['version'] == 2
    assert second['trip_change']['changed_days'] == [3]
    assert second['active_itinerary']['itinerary']['days'][0] == first['active_itinerary']['itinerary']['days'][0]
    assert second['active_itinerary']['itinerary']['preferences']['pace'] == 'relaxed'
    assert second['active_itinerary']['itinerary']['preferences']['audience'] == 'young_travelers'
    assert second['answer'].startswith('## Five days in Japan')
    assert '### Day 5:' in second['answer']
    third = await runtime.execute('alice', 'japan', 'Ahora resume la última versión en español.')
    assert third['active_itinerary']['version'] == 2
    assert 'Resumen' in third['answer']
    assert models.calls[-1][1]['current_active_itinerary']['days'][2]['title'] == 'Nara and a relaxed food walk'
    assert len(store.itinerary_versions('alice','japan')) == 2
    assert store.active_itinerary('alice','other') is None
    assert store.active_itinerary('other','japan') is None


@pytest.mark.asyncio
async def test_tool_summary_cannot_become_revision_answer(environment):
    store, registry, make = environment
    store.save_itinerary('u','s',validate_itinerary(example_trip()))
    models = SequenceModels([
        decision('revise', itinerary=None, mode='tools', skills=[{'name':'place-discovery','arguments':{}}]),
    ], repairs=[{'itinerary': {'changes':[{'day':2, 'title':'Local creative district',
                                            'activities':['Visit art studios','Sample local cuisine']}],
                               'preferences': {'audience':'young_travelers'}}}])
    async def mocked_skill(name, ctx, args):
        return SkillOutput(name=name, text='Retrieved 30 mapped places.', data={'results':[{'name':'A place'}]})
    registry.run=mocked_skill
    result=await make(models).execute('u','s','Add youthful places and keep the days relaxed.')
    assert result['planner_mode']=='model_tools'
    assert result['trip_change']['changed_days']==[2]
    assert result['active_itinerary']['version']==2
    assert 'Retrieved 30 mapped places' not in result['answer']
    assert [c[0] for c in models.calls]==['router','reasoning','synthesis']


@pytest.mark.asyncio
async def test_invalid_revision_never_mutates_saved_trip(environment):
    store, _, make=environment
    original=validate_itinerary(example_trip())
    store.save_itinerary('u','s',original)
    models=SequenceModels([decision('revise', {'changes':[{'day':1,'title':'Empty day','activities':[]}]})],
                          repairs=[{'itinerary': {'changes':[{'day':1,'title':'Empty day','activities':[]}]}}])
    result=await make(models).execute('u','s','Revise the first day.')
    assert result['active_itinerary']['version']==1
    assert result['trip_change'] is None
    assert 'not' in result['answer'].lower()
    assert store.active_itinerary('u','s')['itinerary']==original


@pytest.mark.asyncio
async def test_noop_revision_rejected(environment):
    store, _, make=environment
    original=validate_itinerary(example_trip())
    store.save_itinerary('u','s',original)
    result=await make(SequenceModels([decision('revise', original)])).execute('u','s','Revise the itinerary.')
    assert result['active_itinerary']['version']==1
    assert result['trip_change'] is None


@pytest.mark.asyncio
async def test_legacy_session_recovers_old_itinerary(environment):
    store, _, make=environment
    store.add_message('u','legacy','assistant','## Original trip\n\n' + 'Day 1 has sightseeing and dining. '*20)
    revised = example_trip()
    revised['days'][0]['title'] = 'Updated first day'
    models=SequenceModels([decision('revise', revised)],
        repairs=[{'itinerary': example_trip()}])
    result=await make(models).execute('u','legacy','Revise the first day.')
    assert result['trip_change']['changed_days']==[1]
    assert result['active_itinerary']['version']==1
    assert [x[0] for x in models.calls]==['router','reasoning']


def test_version_is_atomic_scoped_and_delete_cascades(tmp_path):
    store=MemoryStore(tmp_path/'version.sqlite3')
    try:
        trip=validate_itinerary(example_trip())
        store.save_itinerary('alice','one',trip)
        with pytest.raises(ValueError):
            store.save_itinerary('alice','one',trip,expected_version=0)
        assert store.active_itinerary('alice','one')['version']==1
        store.save_itinerary('bob','one',trip)
        store.save_itinerary('alice','two',trip)
        store.delete_user('alice')
        assert store.active_itinerary('alice','one') is None
        assert store.active_itinerary('alice','two') is None
        assert store.active_itinerary('bob','one')['version']==1
    finally:store.close()


def test_revision_requires_matching_duration_and_destination():
    old=validate_itinerary(example_trip())
    shorter={'destination':'Japan','days':old['days'][:-1]}
    with pytest.raises(TripValidationError):
        validate_itinerary(shorter, old, 'revise')
    changed={'destination':'Canada','days':old['days']}
    with pytest.raises(TripValidationError):
        validate_itinerary(changed, old, 'revise')
    with pytest.raises(TripValidationError):
        validate_itinerary({'changes':[{'day':9, 'title':'Other','activities':['Walk']}]},old,'revise')

@pytest.mark.asyncio
async def test_unsupported_optional_tool_does_not_block_revision(environment):
    store, _, make=environment
    store.save_itinerary('u','s',validate_itinerary(example_trip()))
    changed={'changes':[{'day':5,'title':'Relaxed departure',
                         'activities':['Enjoy a calm breakfast','Leave time for departure']}]}
    models=SequenceModels([decision('revise',changed,mode='tools',skills=[{'name':'nonexistent-skill'}])])
    result=await make(models).execute('u','s','Make the final day more relaxing.')
    assert result['planner_mode']=='model_direct_no_tools'
    assert result['active_itinerary']['version']==2
    assert result['trip_change']['changed_days']==[5]


@pytest.mark.asyncio
async def test_api_active_trip_returns_current_version(tmp_path):
    from httpx import ASGITransport, AsyncClient
    from app.api import create_app
    app=create_app(database_path=tmp_path/'api_trip.sqlite3')
    try:
        app.state.store.save_itinerary('local-demo','session-1',validate_itinerary(example_trip()))
        async with AsyncClient(transport=ASGITransport(app=app),base_url='http://testclient') as client:
            ok=await client.get('/api/trips/active',params={'user_id':'local-demo','session_id':'session-1'})
            missing=await client.get('/api/trips/active',params={'user_id':'local-demo','session_id':'session-2'})
        assert ok.status_code==200
        assert ok.json()['active_itinerary']['version']==1
        assert ok.json()['versions'][0]['version']==1
        assert missing.json()['active_itinerary'] is None
    finally:app.state.store.close()


def test_all_distributed_code_is_free_of_chinese_characters():
    """Include examples and evaluation fixtures, not only runtime modules."""
    import re
    roots=['app','core','intelligence','planning','services','storage','quality','models','evaluation','maintenance','startup','web/src','plugins','tests','config','docs','benchmarks']
    for item in roots:
        for path in (ROOT/item).rglob('*'):
            if not path.is_file() or path.suffix not in ('.py','.json','.ts','.tsx','.js','.md','.yaml','.yml','.ps1','.css'):
                continue
            source=path.read_text('utf-8')
            assert not any(0x3400 <= ord(ch) <= 0x9fff for ch in source),str(path)
            escaped_marker=chr(92)+'u'
            assert not re.search(re.escape(escaped_marker)+r'[34][0-9a-fA-F]{3}',source),str(path)

@pytest.mark.asyncio
async def test_original_five_turn_failure_sequence_is_fixed(environment):
    """Plan, multilingual summary, tool-backed edit, edit again, multilingual summary."""
    store, registry, make=environment
    original=example_trip()
    changed_four={'day':4, 'title':'Osaka creative neighborhoods',
                  'activities':['Discover a local design district','Try an informal street-food walk']}
    changed_two={'day':2, 'title':'Western Kyoto without rushing',
                 'activities':['Explore a nearby riverside area','Relax in a neighborhood cafe']}
    models=SequenceModels([
        decision('create', original),
        decision('discuss', answer='Resumen en español de los cinco días.'),
        decision('revise', {'changes':[changed_four]}, mode='tools',
                 skills=[{'name':'place-discovery','arguments':{}}]),
        decision('revise', {'changes':[changed_two], 'preferences':{'pace':'very_relaxed'}}),
        decision('discuss', answer='La última versión conserva cinco días con un ritmo tranquilo.')
    ])
    async def place_stub(name,ctx,args):
        return SkillOutput(name=name,text='Retrieved 30 mapped places.',
                           data={'results':[{'name':'Sample place'}]})
    registry.run=place_stub
    runtime=make(models)
    replies=[]
    for request in (
        'Design a relaxed five-day trip in Japan.',
        'Ahora resume el itinerario en español.',
        'Make this itinerary more interesting for young travelers.',
        'Revise the existing plan to avoid unnecessary travel between cities.',
        'Ahora resume la última versión y conserva mis preferencias.'
    ):
        replies.append(await runtime.execute('u','journey',request))
    assert [row['planner_mode'] for row in replies]==[
        'model_direct','model_direct','model_tools','model_direct','model_direct']
    assert [row['active_itinerary']['version'] for row in replies]==[1,1,2,3,3]
    assert replies[2]['trip_change']['changed_days']==[4]
    assert replies[3]['trip_change']['changed_days']==[2]
    assert 'Retrieved 30 mapped places' not in replies[2]['answer']
    assert '### Day 5' in replies[2]['answer']
    assert 'La última versión' in replies[4]['answer']
    assert models.calls[-1][1]['current_active_itinerary']['days'][3]['title']==changed_four['title']
    assert models.calls[-1][1]['current_active_itinerary']['days'][1]['title']==changed_two['title']
