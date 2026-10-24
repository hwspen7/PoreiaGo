"""Regression checks for session isolation and diagnostic model failures."""
import json
import re
from dataclasses import replace
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from aligo.api import create_app
from aligo.config import settings
from aligo.models import ModelUnavailable
from aligo.runtime import safe_model_issue


class UnavailableAfterRouting:
    def configured(self, role):
        return role in {'router', 'reasoning'}

    def daily_planning_enabled(self):
        return False

    def router_decision_only(self):
        return True

    async def complete(self, role, system, prompt, json_mode=False, **kwargs):
        if role == 'router':
            return {'operation': 'create', 'mode': 'direct', 'itinerary': None,
                    'answer': '', 'fields': {'destination': 'Bristol', 'days': 3},
                    'goals': [], 'skills': []}
        raise ModelUnavailable('ollama-reasoning: HTTP 404')


def events_from(response):
    return [(block.split('event: ')[1].split('\n')[0],
             json.loads(block.split('data: ')[1].split('\n')[0]))
            for block in response.text.replace('\r\n', '\n').split('\n\n')
            if 'event: ' in block and 'data: ' in block]


@pytest.mark.parametrize('issue,code', [
    ('ollama-router: HTTP 404', 'MODEL_OR_ENDPOINT_NOT_FOUND_HTTP_404'),
    ('ollama-reasoning: HTTP 400', 'MODEL_REQUEST_REJECTED'),
    ('ollama-reasoning: HTTP 422', 'MODEL_REQUEST_REJECTED'),
    ('ollama-reasoning: HTTP 429', 'RATE_LIMITED_HTTP_429'),
    ('ollama-reasoning: HTTP 401', 'AUTHENTICATION_FAILED'),
    ('ollama-reasoning: HTTP 503', 'MODEL_PROVIDER_SERVER_ERROR'),
    ('ollama-reasoning: OUTPUT_TRUNCATED', 'OUTPUT_TRUNCATED'),
    ('ollama-reasoning: INVALID_MODEL_JSON', 'INVALID_MODEL_JSON'),
    ('ollama-reasoning: MODEL_TIMEOUT', 'MODEL_TIMEOUT'),
])
def test_distinguishes_provider_failure_families(issue, code):
    assert safe_model_issue(ModelUnavailable(issue)) == code


@pytest.mark.asyncio
async def test_failed_generation_exposes_safe_stage_and_keeps_journeys_separate(tmp_path):
    app = create_app(database_path=tmp_path / 'trips.sqlite3')
    app.state.runtime.models = UnavailableAfterRouting()
    app.state.runtime.settings = replace(settings, agent_mode='model-first', allow_online=False)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://testclient') as client:
            response = await client.post('/api/chat/stream', json={
                'user_id': 'alice', 'session_id': 'first',
                'message': 'Plan a three-day journey in Bristol.'})
            assert response.status_code == 200
            stream = events_from(response)
            assert ('progress', {'stage': 'router'}) in stream
            assert ('progress', {'stage': 'itinerary_generation'}) in stream
            done = [data for kind, data in stream if kind == 'done']
            assert len(done) == 1
            payload = done[0]
            assert payload['model_issue'] == 'MODEL_OR_ENDPOINT_NOT_FOUND_HTTP_404'
            assert payload['model_diagnostic'] == {
                'stage': 'itinerary_generation',
                'code': 'MODEL_OR_ENDPOINT_NOT_FOUND_HTTP_404',
            }
            assert payload['trip_change'] is None
            assert payload['active_itinerary'] is None
            first = (await client.get('/api/history', params={'user_id': 'alice', 'session_id': 'first'})).json()
            second = (await client.get('/api/history', params={'user_id': 'alice', 'session_id': 'second'})).json()
            assert first['messages'][-1]['role'] == 'assistant'
            assert second['messages'] == []
    finally:
        app.state.store.close()


def test_frontend_scopes_pending_state_to_session():
    # Frontend bundle is compiled with tsc independently; verify the boundary
    # against a future accidental regression to global load/message state.
    text = (Path(__file__).resolve().parents[1] / 'web/src/main.tsx').read_text(encoding='utf-8')
    assert 'pendingSessions.current.has(target)' in text
    # Both forms are valid TypeScript; formatting tools may add arrow-parameter parentheses.
    assert re.search(r'\bupdateJourney\s*\(\s*target\s*,\s*(?:state|\(\s*state\s*\))\s*=>', text)
    # The displayed messages may be read directly or filtered through useMemo.
    # Both variants must derive the conversation from the selected session.
    direct_session_messages = re.search(
        r'\bconst\s+messages\s*=\s*selected\?\.messages\s*\?\?\s*\[\s*\]', text
    )
    filtered_session_messages = re.search(
        r'\bconst\s+messages\s*=\s*useMemo\s*\(\s*\(\s*\)\s*=>\s*'
        r'\(\s*selected\?\.messages\s*\?\?\s*\[\s*\]\s*\)\.filter\s*\(', text
    )
    assert direct_session_messages or filtered_session_messages
    assert 'const loading = selected?.loading ?? false' in text
    assert 'sessionRef.current = id' in text
    assert "setMessages(old =>" not in text
    assert 'setLoading(true)' not in text
