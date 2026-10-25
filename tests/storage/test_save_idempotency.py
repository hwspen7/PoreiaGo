"""Repeated Save must not create another copy of the same itinerary version."""
from concurrent.futures import ThreadPoolExecutor
import sqlite3

from storage.memory import MemoryStore


def itinerary():
    return {
        'destination': 'Example City',
        'title': 'Short city trip',
        'days': [{'day': 1, 'title': 'Day 1', 'activities': [
            {'description': 'Explore a museum', 'start_time': '09:00', 'duration_minutes': 60},
            {'description': 'Visit a park', 'start_time': '13:00', 'duration_minutes': 60},
        ]}],
    }


def test_repeated_verified_save_reuses_first_snapshot(tmp_path):
    with MemoryStore(tmp_path / 'one.sqlite3') as store:
        store.save_itinerary('u', 'a', itinerary())
        first = store.save_visible_itinerary('u', 'a', expected_verified_version=1,
                                            expected_draft_version=0)
        again = store.save_visible_itinerary('u', 'a', expected_verified_version=1,
                                            expected_draft_version=0)
        assert first['saved_id'] == again['saved_id']
        assert first['saved_at'] == again['saved_at']
        assert first['already_saved'] is False
        assert again['already_saved'] is True
        assert len(store.saved_trip_snapshots('u', 'a')) == 1
        assert store.saved_trip_snapshots('u', 'other') == []


def test_draft_save_reuses_copy_but_new_draft_version_can_be_saved(tmp_path):
    with MemoryStore(tmp_path / 'draft.sqlite3') as store:
        base = itinerary()
        store.save_itinerary('u', 'a', base)
        old = store.save_visible_itinerary('u', 'a')
        changed = itinerary()
        changed['days'][0]['activities'][1]['start_time'] = '14:00'
        store.save_working_itinerary('u', 'a', changed, base_version=1, previous=base)
        first = store.save_visible_itinerary('u', 'a')
        again = store.save_visible_itinerary('u', 'a')
        assert first['source_status'] == 'draft'
        assert first['saved_id'] == again['saved_id']
        changed2 = itinerary()
        changed2['days'][0]['activities'][1]['start_time'] = '15:00'
        store.save_working_itinerary('u', 'a', changed2, base_version=1,
                                     expected_draft_version=1, previous=changed)
        newest = store.save_visible_itinerary('u', 'a')
        assert newest['saved_id'] not in (old['saved_id'], first['saved_id'])
        assert len(store.saved_trip_snapshots('u', 'a')) == 3


def test_existing_legacy_duplicates_no_longer_multiply(tmp_path):
    with MemoryStore(tmp_path / 'legacy.sqlite3') as store:
        store.save_itinerary('u', 'a', itinerary())
        first = store.save_visible_itinerary('u', 'a')
        # An older app version could insert duplicate rows for the same source.
        with store._connection:
            store._connection.execute('''INSERT INTO saved_trip_snapshots
                (user_id,session_id,source_status,source_version,document,issues,saved_at)
                SELECT user_id,session_id,source_status,source_version,document,issues,saved_at
                FROM saved_trip_snapshots WHERE id=?''', (first['saved_id'],))
        before = store.saved_trip_snapshots('u', 'a')
        again = store.save_visible_itinerary('u', 'a')
        assert len(store.saved_trip_snapshots('u', 'a')) == len(before)
        assert again['saved_id'] == before[0]['id']


def test_concurrent_requests_have_single_snapshot(tmp_path):
    filename = tmp_path / 'concurrent.sqlite3'
    with MemoryStore(filename) as store:
        store.save_itinerary('u', 'a', itinerary())
    def save():
        with MemoryStore(filename) as store:
            return store.save_visible_itinerary('u', 'a')['saved_id']
    with ThreadPoolExecutor(max_workers=2) as pool:
        result = list(pool.map(lambda _: save(), range(2)))
    assert result[0] == result[1]
    with MemoryStore(filename) as store:
        assert len(store.saved_trip_snapshots('u', 'a')) == 1

async def _post_save_twice(app):
    from httpx import ASGITransport, AsyncClient
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://testclient') as client:
        payload = {'user_id': 'u', 'session_id': 'a',
                   'expected_verified_version': 1, 'expected_draft_version': 0}
        first = await client.post('/api/trips/save', json=payload)
        second = await client.post('/api/trips/save', json=payload)
        assert first.status_code == 200 and second.status_code == 200
        assert first.json()['saved_id'] == second.json()['saved_id']
        assert first.json()['saved_at'] == second.json()['saved_at']
        assert first.json()['already_saved'] is False
        assert second.json()['already_saved'] is True


def test_api_repeated_save_reuses_same_id(tmp_path):
    import asyncio
    from app.api import create_app
    app = create_app(database_path=tmp_path / 'api.sqlite3')
    try:
        app.state.store.save_itinerary('u', 'a', itinerary())
        asyncio.run(_post_save_twice(app))
        assert len(app.state.store.saved_trip_snapshots('u', 'a')) == 1
    finally:
        app.state.store.close()
