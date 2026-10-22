"""End-to-end frontend delivery checks with deterministic model responses."""
from __future__ import annotations

import json
from dataclasses import replace

import pytest
from httpx import ASGITransport, AsyncClient

from aligo.api import create_app
from aligo.config import settings


class TripModel:
    """Router and detailed planning stub, not a travel-quality evaluator."""

    def __init__(self):
        self.calls = []

    def configured(self, role):
        return role == 'router'

    def daily_planning_enabled(self):
        return True

    async def complete(self, role, system, prompt, json_mode=False):
        self.calls.append((role, prompt))
        if role == 'router':
            return {'operation': 'create', 'mode': 'direct', 'itinerary': None,
                    'answer': '', 'fields': {'destination': 'Columbus', 'days': 3},
                    'goals': ['culture', 'local food'], 'skills': []}
        data = json.loads(prompt)
        if 'day_number' not in data:
            return {'blueprint': {'destination': 'Columbus', 'title': 'Columbus in Three Days',
                    'summary': 'Neighborhood culture and local dining at a relaxed pace.',
                    'days': [
                        {'day': n, 'city': 'Columbus', 'area': f'Neighborhood {n}',
                         'theme': f'Local culture day {n}', 'anchors': [f'Gallery {n}'],
                         'overnight': 'Columbus', 'transfer': 'Stay in one area',
                         'day_type': 'arrival' if n == 1 else 'departure' if n == 3 else 'sightseeing'}
                        for n in range(1, 4)]}}
        n = data['day_number']
        return {'days': [{'day': n, 'title': f'Local culture day {n}',
                 'area_cluster': f'Neighborhood {n}',
                 'logistics': 'Walk between adjacent venues.',
                 'weather_alternative': 'Choose an indoor gallery if it rains.',
                 'activities': [
                     {'description': f'Explore historic neighborhood {n}', 'time': 'Morning',
                      'why_this_fits': 'Discover the local architectural character.'},
                     {'description': f'Try neighborhood cuisine {n}', 'time': 'Afternoon',
                      'why_this_fits': 'Sample local food at a relaxed pace.'},
                     {'description': f'Visit a cultural gallery {n}', 'time': 'Evening',
                      'why_this_fits': 'Enjoy a different aspect of local culture.'}]}]}


def extract_events(body):
    events = []
    for block in body.replace('\r\n', '\n').split('\n\n'):
        name = None
        data = []
        for line in block.splitlines():
            if line.startswith('event: '):
                name = line[7:]
            if line.startswith('data: '):
                data.append(line[6:])
        if name and data:
            events.append((name, json.loads(''.join(data))))
    return events


@pytest.mark.asyncio
async def test_frontend_stream_returns_renderable_and_persisted_trip(tmp_path):
    app = create_app(database_path=tmp_path / 'frontend.sqlite3')
    model = TripModel()
    app.state.runtime.models = model
    app.state.runtime.settings = replace(settings, agent_mode='model-first', allow_online=False)
    req = {'message': 'Plan a three-day trip in Columbus with culture and local food.',
           'user_id': 'web-smoke', 'session_id': 'trip-one'}
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://testclient') as client:
            async with client.stream('POST', '/api/chat/stream', json=req) as response:
                assert response.status_code == 200
                assert response.headers['content-type'].startswith('text/event-stream')
                events = extract_events(''.join([chunk async for chunk in response.aiter_text()]))
            done = [v for k, v in events if k == 'done']
            assert [k for k, _ in events].count('plan') == 1
            assert len(done) == 1
            assert not any(k == 'error' for k, _ in events)
            payload = done[0]
            assert payload['answer'].startswith('## Columbus in Three Days')
            assert 'Day 3' in payload['answer']
            assert payload['trip_change']['action'] == 'created'
            assert payload['active_itinerary']['version'] == 1
            assert len(payload['active_itinerary']['itinerary']['days']) == 3
            assert payload['session_id'] == 'trip-one'
            active = (await client.get('/api/trips/active', params={'user_id': 'web-smoke', 'session_id': 'trip-one'})).json()
            assert active['active_itinerary'] == payload['active_itinerary']
            history = (await client.get('/api/history', params={'user_id': 'web-smoke', 'session_id': 'trip-one'})).json()
            assert history['messages'][-1]['content'] == payload['answer']
            other = (await client.get('/api/trips/active', params={'user_id': 'web-smoke', 'session_id': 'other'})).json()
            assert other['active_itinerary'] is None
    finally:
        app.state.store.close()


@pytest.mark.asyncio
async def test_frontend_http_and_stream_use_same_response_contract(tmp_path):
    app = create_app(database_path=tmp_path / 'normal.sqlite3')
    model = TripModel()
    app.state.runtime.models = model
    app.state.runtime.settings = replace(settings, agent_mode='model-first', allow_online=False)
    req = {'message': 'Plan a three-day trip in Columbus with culture and local food.',
           'user_id': 'web-smoke', 'session_id': 'nonstream'}
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://testclient') as client:
            result = await client.post('/api/chat', json=req)
            assert result.status_code == 200
            payload = result.json()
            assert payload['trip_change']['action'] == 'created'
            assert payload['active_itinerary']['itinerary']['destination'] == 'Columbus'
            assert len(payload['active_itinerary']['itinerary']['days']) == 3
            assert payload['answer'].startswith('## Columbus in Three Days')
    finally:
        app.state.store.close()


@pytest.mark.asyncio
async def test_frontend_stream_reports_unhandled_backend_errors_as_error_events(tmp_path):
    app = create_app(database_path=tmp_path / 'failure.sqlite3')
    async def broken(*args, **kwargs):
        raise RuntimeError('Untrusted internal error details must not reach browser.')
    app.state.runtime.execute = broken
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://testclient') as client:
            async with client.stream('POST', '/api/chat/stream', json={
                    'message': 'Plan a three-day trip.', 'user_id': 'web-smoke',
                    'session_id': 'broken'}) as response:
                events = extract_events(''.join([chunk async for chunk in response.aiter_text()]))
        assert not any(kind == 'done' for kind, _ in events)
        assert [kind for kind, _ in events if kind == 'error'] == ['error']
        assert 'internal error details' not in json.dumps(events)
    finally:
        app.state.store.close()
