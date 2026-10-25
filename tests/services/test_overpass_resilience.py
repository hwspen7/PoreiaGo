"""Offline regression tests for public Overpass instance resilience."""
from dataclasses import replace

import httpx
import pytest

from core.config import settings
from core.contracts import Evidence
from services.providers import ProviderGateway, ProviderResult, ProviderUnavailable


@pytest.fixture(autouse=True)
def _isolate_geoapify(monkeypatch):
    monkeypatch.delenv('GEOAPIFY_API_KEY', raising=False)


def _gateway(tmp_path):
    return ProviderGateway(replace(settings, allow_online=True,
                                   provider_file=tmp_path / 'none.json',
                                   request_timeout=2.0))


def _error(status):
    request = httpx.Request('POST', 'https://overpass-api.de/api/interpreter')
    return httpx.HTTPStatusError('Remote status', request=request,
                                 response=httpx.Response(status, request=request))


@pytest.mark.asyncio
async def test_temporary_server_error_uses_distinct_instance(tmp_path, monkeypatch):
    gateway = _gateway(tmp_path)
    attempts = []

    async def primary(**kwargs):
        attempts.append('primary')
        raise _error(503)

    async def backup(**kwargs):
        attempts.append('backup')
        return ProviderResult({'results': [{'name': 'Verified map feature'}]},
                              [Evidence(provider='backup-overpass')])

    monkeypatch.setattr(gateway, 'places', primary)
    monkeypatch.setattr(gateway, 'places_backup', backup)
    gateway.register('places', 'openstreetmap', gateway.places, replace=True)
    gateway.fallbacks['places'] = [('backup', gateway.places_backup, None)]
    result = await gateway.request('places', latitude=47.0, longitude=8.0)
    assert attempts == ['primary', 'backup']
    assert result.data['results'][0]['name'] == 'Verified map feature'


@pytest.mark.asyncio
@pytest.mark.parametrize('status', [400, 403, 429])
async def test_do_not_bypass_rejections_or_rate_limits(tmp_path, status):
    gateway = _gateway(tmp_path)
    calls = []

    async def rejected(**kwargs):
        calls.append('primary')
        raise _error(status)

    async def backup(**kwargs):
        calls.append('backup')
        return ProviderResult({'results': []}, [])

    gateway.register('places', 'primary', rejected, replace=True)
    gateway.fallbacks['places'] = [('backup', backup, None)]
    with pytest.raises(ProviderUnavailable, match=f'HTTP {status}'):
        await gateway.request('places', latitude=47.0, longitude=8.0)
    assert calls == ['primary']


@pytest.mark.asyncio
async def test_network_timeout_can_use_backup(tmp_path):
    gateway = _gateway(tmp_path)
    async def primary(**kwargs):
        raise httpx.ConnectError('Connection failed')
    async def backup(**kwargs):
        return ProviderResult({'results': []}, [Evidence(provider='overpass-backup')])
    gateway.register('places', 'primary', primary, replace=True)
    gateway.fallbacks['places'] = [('backup', backup, None)]
    result = await gateway.request('places', latitude=47.0, longitude=8.0)
    assert result.evidence[0].provider == 'overpass-backup'


@pytest.mark.asyncio
async def test_actual_payload_shape_and_endpoint_configuration(tmp_path, monkeypatch):
    gateway = _gateway(tmp_path)
    requests = []
    original = httpx.AsyncClient

    def responder(request):
        requests.append(request)
        return httpx.Response(200, json={'elements': [
            {'type': 'node', 'id': 42, 'lat': 47.0, 'lon': 8.0,
             'tags': {'name': 'A real returned feature', 'tourism': 'viewpoint'}}
        ]})

    transport = httpx.MockTransport(responder)

    def client(*args, **kwargs):
        kwargs['transport'] = transport
        return original(*args, **kwargs)

    monkeypatch.setattr(httpx, 'AsyncClient', client)
    result = await gateway.request('places', latitude=47.0, longitude=8.0, kind='photo')
    assert result.data['results'][0]['osm_id'] == 42
    assert len(requests) == 1
    assert requests[0].headers.get('User-Agent', '').startswith('PoreiaGo')
    assert requests[0].method == 'POST'


@pytest.mark.asyncio
async def test_bad_coordinates_fail_fast(tmp_path):
    gateway = _gateway(tmp_path)
    with pytest.raises(ValueError, match='Latitude'):
        await gateway.places(latitude=95.0, longitude=0.0)
