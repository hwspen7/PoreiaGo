"""Negative security and session-boundary regressions for the local runtime."""
from dataclasses import replace
import json
import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
import app.api as api_module
from core.contracts import ChatRequest, SkillRequest
from core.security import guard_worker_configuration


@pytest.mark.asyncio
async def test_skill_reload_denied_without_admin_and_with_wrong_password(tmp_path, monkeypatch):
    monkeypatch.delenv('POREIAGO_ENABLE_SKILL_ADMIN', raising=False)
    monkeypatch.delenv('POREIAGO_SKILL_ADMIN_TOKEN', raising=False)
    app = api_module.create_app(database_path=tmp_path/'test.db')
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://testclient') as client:
        assert (await client.post('/api/admin/reload-skills')).status_code == 403
        monkeypatch.setenv('POREIAGO_ENABLE_SKILL_ADMIN', 'true')
        monkeypatch.setenv('POREIAGO_SKILL_ADMIN_TOKEN', 'right-key')
        assert (await client.post('/api/admin/reload-skills', headers={'X-PoreiaGo-Admin-Token':'wrong'})).status_code == 401
        allowed = await client.post('/api/admin/reload-skills', headers={'X-PoreiaGo-Admin-Token':'right-key'})
        assert allowed.status_code == 200 and allowed.json()['count'] == 18


@pytest.mark.asyncio
async def test_shared_remote_token_is_single_owner_only(tmp_path, monkeypatch):
    monkeypatch.setattr(api_module, 'settings', replace(api_module.settings, access_token='remote-secret'))
    monkeypatch.setenv('POREIAGO_ALLOW_SHARED_REMOTE','1')
    monkeypatch.setenv('POREIAGO_REMOTE_OWNER_ID','alice')
    app = api_module.create_app(database_path=tmp_path/'remote.db')
    async with AsyncClient(transport=ASGITransport(app=app, client=('203.0.113.8',10001)),
                           base_url='http://example.com', headers={'Authorization':'Bearer remote-secret'}) as client:
        assert (await client.get('/api/history?user_id=bob')).status_code == 403
        assert (await client.get('/api/history')).status_code == 403
        assert (await client.get('/api/history?user_id=alice')).status_code == 200
        assert (await client.delete('/api/users/bob/data')).status_code == 403
        assert (await client.delete('/api/users/alice/data')).status_code == 200
        denied = await client.post('/api/preferences', json={
            'user_id':'bob','category':'pace','value':'slow','consent':True})
        assert denied.status_code == 403
        ok = await client.post('/api/preferences', json={
            'user_id':'alice','category':'pace','value':'slow','consent':True})
        assert ok.status_code == 200
        assert (await client.get('/api/knowledge?user_id=bob')).status_code == 403
        assert (await client.get('/api/knowledge?user_id=alice')).status_code == 200


@pytest.mark.asyncio
async def test_remote_access_denied_without_explicit_enable(tmp_path, monkeypatch):
    monkeypatch.setattr(api_module, 'settings', replace(api_module.settings, access_token='remote-secret'))
    monkeypatch.delenv('POREIAGO_ALLOW_SHARED_REMOTE', raising=False)
    app = api_module.create_app(database_path=tmp_path/'remote.db')
    async with AsyncClient(transport=ASGITransport(app=app, client=('203.0.113.8',10001)),
                           base_url='http://example.com', headers={'Authorization':'Bearer remote-secret'}) as client:
        assert (await client.get('/api/history?user_id=alice')).status_code == 403


@pytest.mark.parametrize('value', ['2', '8'])
def test_reject_multiworker_with_known_worker_environment(monkeypatch, value):
    monkeypatch.setenv('WEB_CONCURRENCY',value)
    with pytest.raises(RuntimeError, match='single-process'):
        guard_worker_configuration()


def test_structured_inputs_have_size_and_depth_bounds():
    with pytest.raises(ValidationError, match='size limit'):
        ChatRequest(message='Plan', context={'huge':'x' * (64 * 1024)})
    with pytest.raises(ValidationError, match='size limit'):
        SkillRequest(arguments={'huge':'x' * (96 * 1024)})
    node = {'level':'end'}
    for _ in range(15):
        node = {'child':node}
    with pytest.raises(ValidationError, match='nesting'):
        ChatRequest(message='Plan', context=node)


@pytest.mark.asyncio
async def test_cors_allows_document_updates(tmp_path):
    app = api_module.create_app(database_path=tmp_path/'cors.db')
    async with AsyncClient(transport=ASGITransport(app=app),base_url='http://testclient') as client:
        r = await client.options('/api/knowledge/documents/1',headers={
            'Origin':'http://127.0.0.1:5173','Access-Control-Request-Method':'PUT'})
        assert r.status_code == 200
        assert 'PUT' in r.headers['access-control-allow-methods']

@pytest.mark.asyncio
async def test_browser_origin_is_enforced_for_local_unsafe_requests(tmp_path):
    app = api_module.create_app(database_path=tmp_path/'csrf.db')
    async with AsyncClient(transport=ASGITransport(app=app),base_url='http://testclient') as client:
        blocked = await client.post('/api/preferences', headers={'Origin':'https://untrusted.example'}, json={
            'user_id':'local-demo','category':'pace','value':'fast','consent':True})
        assert blocked.status_code == 403
        allowed = await client.post('/api/preferences', headers={'Origin':'http://127.0.0.1:5173'}, json={
            'user_id':'local-demo','category':'pace','value':'fast','consent':True})
        assert allowed.status_code == 200


@pytest.mark.asyncio
async def test_cors_preflight_does_not_require_bearer_token(tmp_path, monkeypatch):
    monkeypatch.setattr(api_module, 'settings', replace(api_module.settings, access_token='remote-secret'))
    app = api_module.create_app(database_path=tmp_path/'cors.db')
    async with AsyncClient(transport=ASGITransport(app=app),base_url='http://testclient') as client:
        response = await client.options('/api/chat/stream', headers={
            'Origin':'http://127.0.0.1:5173',
            'Access-Control-Request-Method':'POST',
            'Access-Control-Request-Headers':'authorization,content-type'})
        assert response.status_code == 200
        assert 'authorization' in response.headers['access-control-allow-headers'].lower()


def test_database_process_lock_rejects_competing_process(tmp_path):
    """Two workers must not silently assume an asyncio lock is distributed."""
    import os
    import subprocess
    import sys
    from core.security import guard_database_process
    db = tmp_path / 'shared.db'
    guard_database_process(db)
    code = ('from core.security import guard_database_process;'
            'import sys;'
            'guard_database_process(sys.argv[1])')
    result = subprocess.run([sys.executable, '-c', code, str(db)],
                            capture_output=True, text=True, timeout=8,
                            env={**os.environ, 'PYTHONPATH': str(__import__('pathlib').Path(__file__).resolve().parents[2])})
    assert result.returncode != 0
    assert 'already owned by another process' in result.stderr
