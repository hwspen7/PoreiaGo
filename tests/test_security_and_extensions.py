"""Security boundaries and extension contracts."""
import asyncio
from pathlib import Path
from dataclasses import replace
import pytest
from httpx import AsyncClient,ASGITransport
from aligo.api import create_app
from aligo.config import settings
from aligo.registry import SkillRegistry
from aligo.providers import ProviderGateway,ProviderResult,ProviderUnavailable
from aligo.contracts import Evidence

@pytest.fixture
def app(tmp_path):return create_app(database_path=tmp_path/'security.sqlite3')

@pytest.mark.asyncio
async def test_remote_access_blocked_without_token(app):
    async with AsyncClient(transport=ASGITransport(app=app,client=('203.0.113.2',3000)),base_url='http://example.com') as c:
        response=await c.get('/api/skills')
        assert response.status_code==403

@pytest.mark.asyncio
async def test_document_input_size_is_bounded(app):
    async with AsyncClient(transport=ASGITransport(app=app),base_url='http://testclient') as c:
        response=await c.post('/api/knowledge/documents',json={'user_id':'local-demo','title':'Large','content':'x'*150001})
        assert response.status_code==422

@pytest.mark.asyncio
async def test_skill_catalog_reload(app, monkeypatch):
    async with AsyncClient(transport=ASGITransport(app=app),base_url='http://testclient') as c:
        assert (await c.post('/api/admin/reload-skills')).status_code == 403
        monkeypatch.setenv('ALIGO_ENABLE_SKILL_ADMIN', '1')
        monkeypatch.setenv('ALIGO_SKILL_ADMIN_TOKEN', 'fixture-skill-secret')
        assert (await c.post('/api/admin/reload-skills')).status_code == 401
        response = await c.post('/api/admin/reload-skills', headers={
            'X-Aligo-Admin-Token': 'fixture-skill-secret'})
        assert response.status_code == 200
        assert response.json()['count'] == 18

@pytest.mark.asyncio
async def test_custom_skill_module_entrypoint(tmp_path):
    root=tmp_path/'skills';path=root/'custom-demo';path.mkdir(parents=True)
    import json
    (path/'manifest.json').write_text(json.dumps({'id':'custom-demo','domain':'Extension','description':'Custom handler','wave':'P3','release_state':'implemented','entrypoint':'aligo.handlers:budget'}))
    reg=SkillRegistry(root)
    assert reg.get('custom-demo').entrypoint=='aligo.handlers:budget'

@pytest.mark.asyncio
async def test_retired_navigation_skill_is_not_publicly_available(app):
    waypoints=[{'latitude':36.1,'longitude':120.1},{'latitude':36.2,'longitude':120.2}]
    async with AsyncClient(transport=ASGITransport(app=app),base_url='http://testclient') as c:
        result=await c.post('/api/skills/walking-directions/run',json={'arguments':{'waypoints':waypoints}})
    assert result.status_code==404
    assert result.json()['detail']=='Unknown skill.'

@pytest.mark.asyncio
async def test_trip_scope_overrides_global(app):
    async with AsyncClient(transport=ASGITransport(app=app),base_url='http://testclient') as c:
        for scope,value in [('global','relaxed'),('trip','busy')]:
            response=await c.post('/api/preferences',json={'user_id':'traveler','session_id':'tokyo','category':'pace','value':value,'scope':scope,'consent':True})
            assert response.status_code==200
        tokyo=(await c.get('/api/preferences?user_id=traveler&session_id=tokyo')).json()['preferences']
        paris=(await c.get('/api/preferences?user_id=traveler&session_id=paris')).json()['preferences']
        assert tokyo['pace']['value']=='busy' and paris['pace']['value']=='relaxed'
