"""Regressions for activity-based destinations and coastal trip requests."""
import pytest
from dataclasses import replace
from httpx import ASGITransport, AsyncClient
from app.api import create_app
from core.config import settings
from core.runtime import extract_fields


class OfflineModelStub:
    """Disable model-driven planning during deterministic catalog regression tests."""

    def configured(self, role):
        return False

    async def complete(self, *args, **kwargs):
        raise AssertionError('A live model request is not allowed in this regression test.')

PROMPT=(
    'I want to go on a scenic coastal running trip in Qingdao tomorrow morning. '
    'Plan a 10 km route with beautiful ocean views, photo stops, weather '
    'conditions, temperature, and clothing recommendations.'
)


def test_full_coastal_prompt_fields():
    result=extract_fields(PROMPT)
    assert result['destination']=='Qingdao'
    assert result['activity']=='running'
    assert result['target_distance_km']==10.0
    assert result['date_hint']=='tomorrow morning'


@pytest.mark.parametrize('prompt,destination',[
    ('Plan a 3-day trip from Shanghai to Hangzhou, departing October 23, 2026.', 'Hangzhou'),
    ('I want to go to Vancouver next month.', 'Vancouver'),
    ('Find flights from SFO to AUS on 2026-11-10.', 'AUS'),
    ('I want to go running in Qingdao tomorrow morning.', 'Qingdao'),
    ('Plan a scenic coastal run in Qingdao and recommend clothes.', 'Qingdao'),
])
def test_location_variants(prompt,destination):
    assert extract_fields(prompt)['destination']==destination


@pytest.mark.asyncio
async def test_running_plan_keeps_activity_scope(tmp_path):
    app=create_app(database_path=tmp_path/'regression.sqlite3')
    # Keep regression tests independent of real network availability.
    app.state.providers.settings = replace(settings, allow_online=False)
    # Offline providers alone do not disable cloud models configured in .env.
    # Isolate the catalog behavior from optional LLM tool-selection decisions.
    app.state.runtime.models = OfflineModelStub()
    app.state.models = app.state.runtime.models
    async with AsyncClient(transport=ASGITransport(app=app),base_url='http://testclient') as client:
        response=(await client.post('/api/chat',json={'message':PROMPT})).json()
    assert response['planner_mode']=='catalog_fallback'
    assert response['fields']['destination']=='Qingdao'
    assert response['fields']['target_distance_km']==10
    called={item['name'] for item in response['results']}
    assert 'coastal-running-route' not in called
    assert 'hourly-weather-forecast' in called
    assert 'weather-aware-clothing' not in called
    assert 'multi-day-itinerary-builder' not in called
    assert not any('go on a scenic' in item['text'] for item in response['results'])
