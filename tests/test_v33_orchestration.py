"""Cross-domain orchestration contract tests with deterministic fake Skills."""
from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import pytest

from aligo.config import settings
from aligo.contracts import SkillOutput
from aligo.intelligence import IntentFrame
from aligo.orchestration import BindingError, DependencyUnavailable, bind_arguments, dereference
from aligo.registry import SkillRegistry
from aligo.runtime import Runtime
from aligo.memory import MemoryStore


def fake_registry(tmp_path, definitions):
    for name, options in definitions.items():
        folder = tmp_path / name
        folder.mkdir(parents=True)
        manifest = {
            'id': name, 'description': f'Independent test capability for {name}',
            'domain': 'Integration', 'wave': 'P0', 'release_state': 'implemented',
            'entrypoint': 'aligo.handlers:relaxed_day', 'depends_on': [],
            'input_schema': {'type': 'object', 'additionalProperties': True},
            **options,
        }
        (folder / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
    return SkillRegistry(tmp_path)


class StubModels:
    def configured(self, role):
        return False


class StubProviders:
    pass


async def execute_graph(tmp_path, monkeypatch, definitions, selected, run, *, maximum=8, fields=None):
    reg = fake_registry(tmp_path / 'plugins', definitions)
    reg.run = run
    store = MemoryStore(tmp_path / 'test.sqlite3')
    runsettings = replace(settings, max_skill_calls=maximum, execution_timeout=4, max_parallel_skills=4)
    runtime = Runtime(store, StubModels(), StubProviders(), reg, runsettings, retrieval=object())

    async def fake_intent(*args):
        return IntentFrame(fields=fields or {}, skill_calls=selected)

    monkeypatch.setattr('aligo.runtime.understand_intent', fake_intent)
    events = []
    async def record(name, payload):
        events.append((name, payload))
    result = await runtime.execute('user-one', 'session-one', 'An open-ended travel request', on_event=record)
    store.close()
    return result, events


def test_binding_requires_declared_dependencies(tmp_path):
    reg = fake_registry(tmp_path, {'first': {}, 'second': {'depends_on': ['first'], 'input_bindings': {'forecast': 'first.data.hourly'}}})
    second = reg.get('second')
    bound = bind_arguments(second, {}, {}, {'first': {'status': 'ok', 'data': {'hourly': {'time': ['a']}}}})
    assert bound.arguments['forecast'] == {'time': ['a']}
    assert bound.provenance == {'forecast': 'first.data.hourly'}
    bound.arguments['forecast']['time'].append('changed')
    assert dereference('first.data.hourly', {'first': {'status': 'ok', 'data': {'hourly': {'time': ['a']}}}}) == {'time': ['a']}
    with pytest.raises(BindingError):
        bind_arguments(second, {'forged': {'$from': 'private.data.secret'}}, {}, {'first': {'status': 'ok', 'data': {'hourly': 42}}})
    with pytest.raises(BindingError):
        dereference('first.__class__', {'first': {'status': 'ok', 'data': {}}})
    with pytest.raises(DependencyUnavailable):
        dereference('first.data', {'first': {'status': 'unavailable', 'data': {'hourly': 'unverified'}}})


@pytest.mark.asyncio
async def test_data_binding_and_provenance_reaches_downstream(tmp_path, monkeypatch):
    seen = {}
    async def run(name, ctx, args):
        if name == 'source':
            return SkillOutput(name=name, text='Structured evidence', data={'forecast': {'temperature_c': 11}})
        seen['arguments'] = args
        return SkillOutput(name=name, text='Interpreted available data')
    result, _ = await execute_graph(tmp_path, monkeypatch, {
        'source': {}, 'consumer': {'depends_on': ['source'],
                                     'input_bindings': {'weather': 'source.data.forecast'}},
    }, [{'name': 'consumer'}], run)
    assert seen['arguments']['weather']['temperature_c'] == 11
    assert result['execution_trace'][1]['input_bindings'] == {'weather': 'source.data.forecast'}
    assert result['results'][-1]['status'] == 'ok'


@pytest.mark.asyncio
async def test_explicit_arguments_override_manifest_defaults(tmp_path, monkeypatch):
    seen = []
    async def run(name, ctx, args):
        if name == 'downstream':
            seen.append(args['weather'])
        return SkillOutput(name=name, text='ok', data={'forecast': {'temperature_c': 15}})
    result, _ = await execute_graph(tmp_path, monkeypatch, {
        'upstream': {}, 'downstream': {'depends_on': ['upstream'],
                                        'input_bindings': {'weather': 'upstream.data.forecast'}},
    }, [{'name': 'downstream', 'arguments': {'weather': {'temperature_c': 18}}}], run)
    assert seen == [{'temperature_c': 18}]
    assert result['execution_trace'][1]['input_bindings'] == {}


@pytest.mark.asyncio
async def test_dependency_unavailable_blocks_downstream(tmp_path, monkeypatch):
    invoked = []
    async def run(name, ctx, args):
        invoked.append(name)
        return SkillOutput(name=name, text='Provider disabled', status='unavailable')
    result, events = await execute_graph(tmp_path, monkeypatch, {
        'upstream': {}, 'downstream': {'depends_on': ['upstream']},
    }, [{'name': 'downstream'}], run)
    assert invoked == ['upstream']
    assert result['results'][1]['error_code'] == 'DEPENDENCY_UNAVAILABLE'
    assert [x for x, _ in events if x == 'skill'] == ['skill', 'skill']


@pytest.mark.asyncio
async def test_manifest_declared_fallback_recovers_before_dependents(tmp_path, monkeypatch):
    observed = []
    async def run(name, ctx, args):
        observed.append(name)
        if name == 'primary':
            return SkillOutput(name=name, status='unavailable', text='Unavailable')
        if name == 'backup':
            return SkillOutput(name=name, text='Verified backup', data={'list': ['verified']})
        assert name == 'dependent'
        assert ctx.shared['primary']['recovered_by'] == 'backup'
        return SkillOutput(name=name, text='Uses backup evidence')
    result, events = await execute_graph(tmp_path, monkeypatch, {
        'primary': {'fallback_skills': ['backup']}, 'backup': {},
        'dependent': {'depends_on': ['primary']},
    }, [{'name': 'dependent'}], run)
    assert observed == ['primary', 'backup', 'dependent']
    assert result['recovery'][0]['replacement_skill'] == 'backup'
    assert len([event for event in events if event[0] == 'replan']) == 1
    assert result['execution_trace'][0]['recovered_by'] == 'backup'
    assert result['results'][-1]['status'] == 'ok'


@pytest.mark.asyncio
async def test_recovery_uses_bounded_budget(tmp_path, monkeypatch):
    observed = []
    async def run(name, ctx, args):
        observed.append(name)
        if name == 'primary':
            return SkillOutput(name=name, text='Unavailable', status='unavailable')
        return SkillOutput(name=name, text='ok')
    result, _ = await execute_graph(tmp_path, monkeypatch, {
        'primary': {'fallback_skills': ['backup']}, 'backup': {},
        'dependent': {'depends_on': ['primary']},
    }, [{'name': 'dependent'}], run, maximum=2)
    assert observed == ['primary']
    assert result['recovery'] == []
    assert result['results'][-1]['error_code'] == 'DEPENDENCY_UNAVAILABLE'


@pytest.mark.asyncio
async def test_mutating_fallback_is_never_executed(tmp_path, monkeypatch):
    observed = []
    async def run(name, ctx, args):
        observed.append(name)
        return SkillOutput(name=name, text='Unavailable', status='unavailable')
    result, _ = await execute_graph(tmp_path, monkeypatch, {
        'primary': {'fallback_skills': ['dangerous']}, 'dangerous': {'mutates_state': True},
    }, [{'name': 'primary'}], run)
    assert observed == ['primary']
    assert result['recovery'] == []


@pytest.mark.asyncio
async def test_fallback_failure_does_not_loop(tmp_path, monkeypatch):
    observed = []
    async def run(name, ctx, args):
        observed.append(name)
        return SkillOutput(name=name, status='error', text='Failure')
    result, _ = await execute_graph(tmp_path, monkeypatch, {
        'primary': {'fallback_skills': ['backup']}, 'backup': {'fallback_skills': ['primary']},
    }, [{'name': 'primary'}], run)
    assert observed == ['primary', 'backup']
    assert len(result['recovery']) == 1


@pytest.mark.asyncio
async def test_model_selected_untrusted_binding_cannot_access_other_skill(tmp_path, monkeypatch):
    invoked = []
    async def run(name, ctx, args):
        invoked.append(name)
        return SkillOutput(name=name, text='ok')
    result, _ = await execute_graph(tmp_path, monkeypatch, {
        'a': {}, 'b': {},
    }, [{'name': 'a', 'arguments': {'secret': {'$from': 'b.data.private'}}}], run)
    assert invoked == []
    assert result['results'][0]['error_code'] == 'INPUT_BINDING_UNAVAILABLE'


@pytest.mark.asyncio
async def test_intent_model_falls_back_from_router_to_reasoning(tmp_path, monkeypatch):
    from aligo.intelligence import understand_intent
    registry = fake_registry(tmp_path, {'experience': {}})
    calls = []
    class Models:
        def configured(self, role):
            return role in {'router', 'reasoning'}
        async def complete(self, role, system, prompt, json_mode=False):
            calls.append(role)
            if role == 'router':
                raise ConnectionError('Primary model offline')
            return {'fields': {'interest': 'architecture'}, 'goals': ['architecture tours'],
                    'skills': [{'name': 'experience', 'arguments': {}},
                               {'name': 'unauthorized', 'arguments': {}}]}
    monkeypatch.setenv('ALIGO_INTENT_MODEL_ROLES', 'router,reasoning')
    output = await understand_intent('A general preference', [], {}, Models(), registry, 4)
    assert calls == ['router', 'reasoning']
    assert output.mode == 'model_reasoning'
    assert output.skill_calls == [{'name': 'experience', 'arguments': {}}]
    assert output.fields['interest'] == 'architecture'
    assert output.limitations


@pytest.mark.asyncio
async def test_bad_model_json_falls_back_to_catalog(tmp_path, monkeypatch):
    from aligo.intelligence import understand_intent
    registry = fake_registry(tmp_path, {'custom-activity': {'keywords': ['custom activity']}})
    class Models:
        def configured(self, role):
            return role == 'router'
        async def complete(self, role, system, prompt, json_mode=False):
            return {'skills': [{'name': 'nonexistent', 'arguments': {}}]}
    monkeypatch.setenv('ALIGO_INTENT_MODEL_ROLES', 'router,reasoning')
    output = await understand_intent('Find a custom activity', [], {}, Models(), registry, 4)
    assert output.mode == 'catalog_fallback'
    assert output.skill_calls[0]['name'] == 'custom-activity'
    assert any('did not return' in text for text in output.limitations)


def test_recovery_reports_uncertainty_without_false_failure():
    results = [SkillOutput(name='primary', text='Unavailable', status='unavailable').model_dump(),
               SkillOutput(name='alternate', text='Retrieved verified data', status='ok').model_dump()]
    from aligo.runtime import Runtime
    response = Runtime._synthesize(results, 'query', [{'failed_skill': 'primary', 'replacement_skill': 'alternate'}])
    assert 'Retrieved verified data' in response
    assert 'A declared alternative' in response
    assert 'Not available with the current providers: primary' not in response
