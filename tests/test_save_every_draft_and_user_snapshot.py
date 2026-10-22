"""Persist all draft revisions and allow explicit provisional snapshot saving."""
import json
from dataclasses import replace
from pathlib import Path

import pytest

from aligo.config import settings
from aligo.memory import MemoryStore
from aligo.registry import SkillRegistry
from aligo.runtime import Runtime
from aligo.trip_state import validate_itinerary

ROOT = Path(__file__).resolve().parents[1]


def itinerary():
    return validate_itinerary({'destination': 'Sample City', 'title': 'A draft itinerary',
        'days': [{'day': 1, 'title': 'Day 1', 'activities': [
            {'description': 'Visit a gallery', 'start_time': '09:00', 'duration_minutes': 60},
            {'description': 'Walk in a park', 'start_time': '12:00', 'duration_minutes': 90}
        ]}]})


def test_each_draft_is_archived_and_user_save_keeps_provisional_label(tmp_path):
    filename = tmp_path / 'test.sqlite3'
    store = MemoryStore(filename)
    store.save_itinerary('u', 'journey', itinerary())
    d1 = itinerary()
    d1['days'][0]['activities'][1]['start_time'] = '09:20'
    d2 = itinerary()
    d2['days'][0]['activities'][1]['start_time'] = '09:30'
    store.save_working_itinerary('u', 'journey', d1, base_version=1,
        issues=[{'code': 'SCHEDULE_INVARIANT', 'detail': 'Overlapping visits'}])
    store.save_working_itinerary('u', 'journey', d2, base_version=1, expected_draft_version=1,
        issues=[{'code': 'SCHEDULE_INVARIANT', 'detail': 'Overlapping visits'}])
    assert [x['draft_version'] for x in store.working_itinerary_history('u', 'journey')] == [2, 1]
    assert store.working_itinerary_version('u', 'journey', 1)['itinerary'] == d1
    saved = store.save_visible_itinerary('u', 'journey')
    assert saved['source_status'] == 'draft'
    assert saved['source_version'] == 2
    assert saved['itinerary'] == d2
    assert store.active_itinerary('u', 'journey')['version'] == 1
    assert len(store.itinerary_versions('u', 'journey')) == 1
    assert store.saved_trip_snapshot('u', 'another', saved['saved_id']) is None
    store.close()
    store = MemoryStore(filename)
    assert store.working_itinerary_version('u', 'journey', 1)['itinerary'] == d1
    assert store.saved_trip_snapshot('u', 'journey', saved['saved_id'])['itinerary'] == d2
    store.save_itinerary('u', 'journey', itinerary(), expected_version=1, operation='revise')
    assert store.working_itinerary('u', 'journey') is None
    assert len(store.working_itinerary_history('u', 'journey')) == 2
    assert store.saved_trip_snapshot('u', 'journey', saved['saved_id'])['source_status'] == 'draft'
    another = store.save_working_itinerary('u', 'journey', d1, base_version=2,
                                           expected_draft_version=0)
    assert another['draft_version'] == 3
    assert [r['draft_version'] for r in store.working_itinerary_history('u','journey')] == [3, 2, 1]
    store.close()


def test_full_user_deletion_removes_all_saved_and_draft_versions(tmp_path):
    store = MemoryStore(tmp_path / 'delete.sqlite3')
    store.save_itinerary('u', 'trip', itinerary())
    store.save_working_itinerary('u', 'trip', itinerary(), base_version=1)
    saved = store.save_visible_itinerary('u', 'trip')
    store.save_itinerary('other', 'trip', itinerary())
    store.delete_user('u')
    assert store.active_itinerary('u', 'trip') is None
    assert store.working_itinerary('u', 'trip') is None
    assert store.working_itinerary_history('u', 'trip') == []
    assert store.saved_trip_snapshot('u', 'trip', saved['saved_id']) is None
    assert store.active_itinerary('other', 'trip') is not None
    store.close()


class SaveRouter:
    def configured(self, role):
        return role == 'router'

    def router_decision_only(self):
        return True

    async def complete(self, role, system, prompt, **kwargs):
        assert role == 'router'
        assert json.loads(prompt)['current_active_itinerary']
        return {'operation': 'save', 'mode': 'direct', 'itinerary': None,
                'answer': '', 'fields': {}, 'goals': [], 'skills': []}


@pytest.mark.asyncio
async def test_explicit_chat_save_does_not_certify_a_provisional_draft(tmp_path):
    store = MemoryStore(tmp_path / 'chat.sqlite3')
    store.save_itinerary('u', 'journey', itinerary())
    altered = itinerary()
    altered['days'][0]['activities'][1]['start_time'] = '09:10'
    store.save_working_itinerary('u', 'journey', altered, base_version=1,
        issues=[{'code': 'SCHEDULE_INVARIANT'}])
    runtime = Runtime(store, SaveRouter(), object(),
                      SkillRegistry(ROOT / 'plugins' / 'skills'),
                      replace(settings, agent_mode='model-first'), retrieval=object())
    result = await runtime.execute('u', 'journey', 'Save my current itinerary.')
    assert result['saved_snapshot']['source_status'] == 'draft'
    assert result['working_draft']['draft_version'] == 1
    assert result['active_itinerary']['version'] == 1
    assert 'Saved working draft' in result['answer']
    assert len(store.saved_trip_snapshots('u', 'journey')) == 1
    store.close()


def test_saving_without_a_trip_raises_instead_of_claiming_success(tmp_path):
    store = MemoryStore(tmp_path / 'empty.sqlite3')
    with pytest.raises(ValueError, match='No itinerary'):
        store.save_visible_itinerary('u', 'missing')
    store.close()

@pytest.mark.asyncio
async def test_save_endpoints_preserve_scope_history_and_optimistic_version(tmp_path):
    from httpx import ASGITransport, AsyncClient
    from aligo.api import create_app
    app = create_app(database_path=tmp_path / 'api.sqlite3')
    store = app.state.store
    store.save_itinerary('u', 'j1', itinerary())
    altered = itinerary()
    altered['days'][0]['activities'][1]['start_time'] = '09:10'
    store.save_working_itinerary('u', 'j1', altered, base_version=1)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://testclient') as client:
            response = await client.post('/api/trips/save', json={
                'user_id': 'u', 'session_id': 'j1',
                'expected_verified_version': 1, 'expected_draft_version': 1})
            assert response.status_code == 200
            snapshot = response.json()
            assert snapshot['source_status'] == 'draft'
            assert (await client.get('/api/trips/active', params={
                'user_id': 'u', 'session_id': 'j1'})).json()['draft_history'][0]['draft_version'] == 1
            loaded = await client.get(f"/api/trips/saved/{snapshot['saved_id']}", params={
                'user_id': 'u', 'session_id': 'j1'})
            assert loaded.json()['itinerary'] == altered
            bad_scope = await client.get(f"/api/trips/saved/{snapshot['saved_id']}", params={
                'user_id': 'u', 'session_id': 'j2'})
            assert bad_scope.status_code == 404
            stale = await client.post('/api/trips/save', json={
                'user_id': 'u', 'session_id': 'j1',
                'expected_verified_version': 1, 'expected_draft_version': 0})
            assert stale.status_code == 409
    finally:
        store.close()
