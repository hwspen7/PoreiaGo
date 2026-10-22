"""Secure, persistent model administration and request-snapshot regressions."""
from __future__ import annotations

import json
import os
import sys
import types
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

import aligo.api as api_module
from aligo.model_management import ModelControl, _validate_profile
from aligo.config import settings


def _profile(name, role, model, *, endpoint='http://127.0.0.1:11434/v1', key='LOCAL_KEY'):
    return {'name': name, 'role': role, 'provider': 'openai-compatible',
            'model': model, 'base_url': endpoint, 'api_key_env': key,
            'max_output_tokens': 2048}


def _stages(*, reasoning='brain', router='router', synthesis='synthesis'):
    return {'router': router, 'reasoning': reasoning, 'synthesis': synthesis,
            'blueprint': reasoning, 'daily': reasoning, 'repair': reasoning}


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    monkeypatch.delenv('ALIGO_MODEL_PRESET', raising=False)
    monkeypatch.delenv('ALIGO_MODEL_PRESETS_FILE', raising=False)
    monkeypatch.delenv('ALIGO_LOCAL_MODEL', raising=False)
    monkeypatch.setenv('LOCAL_KEY', 'fixture-secret')
    models_path = tmp_path / 'models.json'
    presets_path = tmp_path / 'model_presets.json'
    state_path = tmp_path / 'active.json'
    profiles = [_profile('router', 'router', 'router-small'),
                _profile('brain', 'reasoning', 'base'),
                _profile('synthesis', 'synthesis', 'base')]
    models_path.write_text(json.dumps({'profiles': profiles}), encoding='utf-8')
    presets_path.write_text(json.dumps({'version': 1, 'presets': {'base': {
        'stages': _stages()}}}), encoding='utf-8')
    configuration = replace(settings, models_file=models_path, db_path=tmp_path / 'test.sqlite3')
    return configuration, models_path, presets_path, state_path


def _control(sandbox):
    config, _, presets, state = sandbox
    return ModelControl(config, presets_file=presets, state_file=state)


def test_preset_activation_and_restart_persistence(sandbox):
    first = _control(sandbox)
    first.add_profile(_profile('large', 'reasoning', 'big-70b'))
    first.add_preset('mixed', {**_stages(), 'daily': {'profile': 'large', 'model': 'big-70b'}})
    gateway, remote = first.prepare_activation('mixed')
    old_gateway = first.gateway
    first.commit_activation('mixed', gateway, remote)
    assert first.active_preset == 'mixed'
    assert old_gateway.selected_profiles() is None
    assert first.gateway.selected_profiles()['daily']['model'] == 'big-70b'
    restarted = _control(sandbox)
    assert restarted.active_preset == 'mixed'
    assert restarted.gateway.selected_profiles()['daily']['profile'] == 'large'
    with pytest.raises(ValueError, match='referenced'):
        restarted.remove_profile('large')
    assert 'fixture-secret' not in json.dumps(restarted.public_state())


def test_reject_unsupported_models_and_unsafe_endpoints():
    for target in ('http://example.com/v1', 'http://192.168.1.2:3000/v1',
                   'https://127.0.0.1:9000@evil.com/v1', 'https://10.0.0.1/v1',
                   'http://evil.local/v1'):
        with pytest.raises(ValueError):
            _validate_profile(_profile('unsafe', 'reasoning', 'm', endpoint=target))
    with pytest.raises(ValueError):
        _validate_profile({**_profile('wrong', 'reasoning', 'm'), 'api_key': 'secret'})
    with pytest.raises(ValueError):
        _validate_profile({**_profile('wrong', 'reasoning', 'm'), 'role': 'embedding'})
    with pytest.raises(ValueError):
        _validate_profile({**_profile('wrong', 'reasoning', 'm'), 'max_output_tokens': True})
    assert _validate_profile(_profile('local-other', 'reasoning', 'm',
                                     endpoint='http://127.0.0.1:8080/v1'))['name'] == 'local-other'


def test_failed_update_does_not_change_catalog(sandbox):
    control = _control(sandbox)
    before = control.presets_file.read_bytes()
    with pytest.raises(ValueError, match='Invalid daily profile'):
        control.add_preset('invalid', {**_stages(), 'daily': 'router'})
    assert control.presets_file.read_bytes() == before
    with pytest.raises(ValueError, match='Unsupported daily'):
        control.add_preset('invalid', {**_stages(), 'daily': {
            'profile': 'brain', 'base_url': 'https://evil.com/v1'}})
    assert control.presets_file.read_bytes() == before


def test_remote_requires_opt_in_and_credential(sandbox, monkeypatch):
    control = _control(sandbox)
    control.add_profile(_profile('cloud', 'reasoning', 'cloud-model',
                         endpoint='https://api.example.com/v1', key='CLOUD_KEY'))
    control.add_preset('remote', _stages(reasoning='cloud'))
    with pytest.raises(PermissionError):
        control.prepare_activation('remote')
    with pytest.raises(ValueError, match='Unconfigured'):
        control.prepare_activation('remote', allow_remote=True)
    monkeypatch.setenv('CLOUD_KEY', 'cloud-secret')
    gateway, remote = control.prepare_activation('remote', allow_remote=True)
    assert 'daily' in remote
    assert not control.state_file.exists()
    control.commit_activation('remote', gateway, remote)
    assert control.state_file.exists()
    assert 'cloud-secret' not in control.state_file.read_text()


@pytest.mark.asyncio
async def test_offline_check_never_creates_a_model_client(sandbox, monkeypatch):
    control = _control(sandbox)
    def forbidden(*args, **kwargs):
        raise AssertionError('Offline preflight must not create a model client.')
    monkeypatch.setitem(sys.modules, 'openai', types.SimpleNamespace(AsyncOpenAI=forbidden))
    result = await control.check_compatibility('base', ['daily', 'router'])
    assert result['status'] == 'configuration_valid'
    assert result['stages']['daily']['status'] == 'configuration_valid'


@pytest.mark.asyncio
async def test_json_smoke_tests_actual_selected_stage_models(sandbox, monkeypatch):
    control = _control(sandbox)
    control.add_profile(_profile('large', 'reasoning', '70b-local',
                                 endpoint='http://127.0.0.1:8080/v1'))
    control.add_preset('local', {**_stages(), 'daily': 'large'})
    seen = []
    class Responses:
        async def create(self, **kwargs):
            seen.append(kwargs)
            return SimpleNamespace(choices=[SimpleNamespace(finish_reason='stop',
                message=SimpleNamespace(content='{"ok": true}'))])
    class Client:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=Responses())
    monkeypatch.setitem(sys.modules, 'openai', types.SimpleNamespace(AsyncOpenAI=Client))
    outcome = await control.check_compatibility('local', ['daily', 'router'], connect=True)
    assert outcome['status'] == 'smoke_pass'
    assert [item['model'] for item in seen] == ['70b-local', 'router-small']
    assert all(item['max_tokens'] == 96 for item in seen)
    assert outcome['stages']['daily']['endpoint'] == 'local-compatible'


def _app(sandbox, monkeypatch, *, management=True, token=True):
    config, profiles, presets, state = sandbox
    monkeypatch.setattr(api_module, 'settings', replace(config, access_token=''))
    monkeypatch.setenv('ALIGO_ENABLE_MODEL_ADMIN', '1' if management else '0')
    if token:
        monkeypatch.setenv('ALIGO_MODEL_ADMIN_TOKEN', 'admin-secret')
    else:
        monkeypatch.delenv('ALIGO_MODEL_ADMIN_TOKEN', raising=False)
    return api_module.create_app(database_path=config.db_path,
        models_file=profiles, model_presets_file=presets, model_state_file=state)


@pytest.mark.asyncio
async def test_api_auth_mutations_activation_and_request_pinning(sandbox, monkeypatch):
    app = _app(sandbox, monkeypatch)
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://testserver') as http:
        headers = {'X-Aligo-Admin-Token': 'admin-secret'}
        res = await http.post('/api/models/profiles', json=_profile('large', 'reasoning', '70b'))
        assert res.status_code == 401
        assert (await http.get('/api/models', headers=headers)).status_code == 200
        added = await http.post('/api/models/profiles', headers=headers,
                                json=_profile('large', 'reasoning', '70b'))
        assert added.status_code == 201, added.text
        created = await http.post('/api/models/presets', headers=headers,
                json={'name': 'large-trip', 'stages': {**_stages(), 'daily': 'large'}})
        assert created.status_code == 201, created.text
        old_runtime = app.state.runtime
        old_models = old_runtime.models
        switched = await http.post('/api/models/activate', headers=headers,
                                   json={'preset': 'large-trip'})
        assert switched.status_code == 200, switched.text
        assert app.state.runtime is not old_runtime
        assert old_runtime.models is old_models
        assert app.state.runtime.models.selected_profiles()['daily']['model'] == '70b'
        assert (await http.get('/api/models', headers=headers)).json()['active_preset'] == 'large-trip'
        assert (await http.post('/api/models/activate', headers=headers,
                json={'preset': 'nonexistent'})).status_code == 422
        assert app.state.runtime.models.selected_profiles()['daily']['model'] == '70b'
        assert (await http.delete('/api/models/profiles/large', headers=headers)).status_code == 409


@pytest.mark.asyncio
async def test_management_is_opt_in_even_on_localhost(sandbox, monkeypatch):
    app = _app(sandbox, monkeypatch, management=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://testserver') as http:
        result = await http.post('/api/models/activate',
                headers={'X-Aligo-Admin-Token': 'admin-secret'}, json={'preset': 'base'})
        assert result.status_code == 403
    app = _app(sandbox, monkeypatch, token=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://testserver') as http:
        result = await http.post('/api/models/activate', json={'preset': 'base'})
        assert result.status_code == 403


@pytest.mark.asyncio
async def test_api_compatible_smoke_test_without_external_network(sandbox, monkeypatch):
    app = _app(sandbox, monkeypatch)
    calls = []
    class Responses:
        async def create(self, **kwargs):
            calls.append(kwargs['model'])
            return SimpleNamespace(choices=[SimpleNamespace(finish_reason='stop',
                message=SimpleNamespace(content='{"ok":true}'))])
    class Client:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=Responses())
    monkeypatch.setitem(sys.modules, 'openai', types.SimpleNamespace(AsyncOpenAI=Client))
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://testserver') as http:
        outcome = await http.post('/api/models/check',
              headers={'X-Aligo-Admin-Token': 'admin-secret'},
              json={'preset': 'base', 'stages': ['daily'], 'connect': True})
        assert outcome.status_code == 200, outcome.text
        assert outcome.json()['status'] == 'smoke_pass'
        assert calls == ['base']


def test_environment_pinned_preset_cannot_be_changed_and_lost_on_restart(sandbox, monkeypatch):
    control = _control(sandbox)
    monkeypatch.setenv('ALIGO_MODEL_PRESET', 'base')
    with pytest.raises(ValueError, match='Unset ALIGO_MODEL_PRESET'):
        control.prepare_activation(None)
    with pytest.raises(ValueError, match='Unset ALIGO_MODEL_PRESET'):
        control.prepare_activation('other')
    assert not control.state_file.exists()


@pytest.mark.asyncio
async def test_chat_started_before_activation_uses_immutable_runtime(sandbox, monkeypatch):
    import asyncio
    app = _app(sandbox, monkeypatch)
    old_runtime = app.state.runtime
    entered, release = asyncio.Event(), asyncio.Event()

    async def old_execute(user_id, session_id, message, context):
        entered.set()
        await release.wait()
        return {'model_generation': 'old', 'message': message}

    old_runtime.execute = old_execute
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://testserver') as http:
        headers = {'X-Aligo-Admin-Token': 'admin-secret'}
        running = asyncio.create_task(http.post('/api/chat', headers=headers,
                        json={'user_id': 'local-demo', 'session_id': 'x', 'message': 'Plan a trip'}))
        await asyncio.wait_for(entered.wait(), timeout=3)
        result = await http.post('/api/models/activate', headers=headers,
                                 json={'preset': 'base'})
        assert result.status_code == 200, result.text
        assert app.state.runtime is not old_runtime
        release.set()
        completed = await running
        assert completed.json()['model_generation'] == 'old'
        assert app.state.runtime.models.selected_profiles()['daily']['model'] == 'base'


def test_cli_rejects_localhost_lookalikes_without_sending_token(monkeypatch):
    from scripts.model_admin_cli import _request
    for url in ('http://localhost:8000@evil.com',
                'http://127.0.0.1:8000.evil.com',
                'https://localhost:8000',
                'http://localhost:8000/v1',
                'http://localhost:8000?next=evil'):
        with pytest.raises(ValueError, match='local Aligo'):
            _request(url, 'secret-token', 'GET', '/api/models')


def test_extended_model_timeout_requires_explicit_opt_in(tmp_path):
    import subprocess
    from pathlib import Path
    base = dict(os.environ)
    base['ALIGO_DAILY_GENERATION_TIMEOUT'] = '750'
    base.pop('ALIGO_EXTENDED_MODEL_TIMEOUT', None)
    command = [sys.executable, '-c',
               'from aligo.config import Settings; print(Settings().daily_generation_timeout)']
    plain = subprocess.check_output(command, env=base, text=True).strip()
    assert float(plain) == 300.0
    base['ALIGO_EXTENDED_MODEL_TIMEOUT'] = '1'
    extended = subprocess.check_output(command, env=base, text=True).strip()
    assert float(extended) == 750.0


@pytest.mark.asyncio
async def test_admin_token_does_not_change_regular_frontend_authorization(sandbox, monkeypatch):
    app = _app(sandbox, monkeypatch)
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://testserver') as http:
        result = await http.get('/api/models')
        assert result.status_code == 200
        assert result.json()['management_enabled'] is True
        attempted = await http.post('/api/models/activate', json={'preset': 'base'})
        assert attempted.status_code == 401
