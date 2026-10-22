"""Offline tests of the optional provider-independent place discovery path."""
from dataclasses import replace

import httpx
import pytest

from aligo.config import settings
from aligo.providers import ProviderGateway, ProviderUnavailable


def gateway(tmp_path):
    return ProviderGateway(replace(settings, allow_online=True,
                                   provider_file=tmp_path / 'empty.json',
                                   request_timeout=2.0))


def geojson():
    return {'type': 'FeatureCollection', 'features': [
        {'type': 'Feature',
         'properties': {'name': 'Sample Viewpoint',
                        'place_id': 'sample-id',
                        'categories': ['tourism.attraction.viewpoint']},
         'geometry': {'type': 'Point', 'coordinates': [8.50, 47.20]}}
    ]}


def test_no_key_retains_free_osm_mode(tmp_path, monkeypatch):
    monkeypatch.delenv('GEOAPIFY_API_KEY', raising=False)
    g = gateway(tmp_path)
    assert g.adapters['places'][0] == 'openstreetmap'
    place = next(item for item in g.catalog() if item['capability'] == 'places')
    assert place['configured'] is True
    assert place['requires_key'] is False


def test_key_selects_geoapify_as_primary(tmp_path, monkeypatch):
    monkeypatch.setenv('GEOAPIFY_API_KEY', 'secret-for-tests')
    g = gateway(tmp_path)
    assert g.adapters['places'][0] == 'geoapify'
    assert [name for name, _, _ in g.fallbacks['places']] == [
        'openstreetmap', 'overpass-private-coffee']
    assert next(x for x in g.catalog() if x['capability'] == 'places')['requires_key']


@pytest.mark.asyncio
async def test_geoapify_response_is_normalized_and_key_stays_out_of_url(tmp_path, monkeypatch):
    monkeypatch.setenv('GEOAPIFY_API_KEY', 'secret-for-tests')
    requests = []
    original = httpx.AsyncClient

    def responder(request):
        requests.append(request)
        return httpx.Response(200, json=geojson())

    transport = httpx.MockTransport(responder)
    def client(*args, **kwargs):
        kwargs['transport'] = transport
        return original(*args, **kwargs)
    monkeypatch.setattr(httpx, 'AsyncClient', client)

    g = gateway(tmp_path)
    out = await g.request('places', latitude=47.20, longitude=8.50,
                          kind='photo', language='de')
    assert out.data['results'][0]['name'] == 'Sample Viewpoint'
    assert out.data['results'][0]['place_id'] == 'sample-id'
    assert out.data['results'][0]['latitude'] == 47.20
    assert out.evidence[0].provider == 'geoapify-places'
    assert requests[0].headers['x-api-key'] == 'secret-for-tests'
    assert 'secret-for-tests' not in str(requests[0].url)
    assert requests[0].url.params['lang'] == 'de'
    assert requests[0].url.params['filter'] == 'circle:8.5,47.2,4500'


@pytest.mark.asyncio
async def test_geoapify_invalid_coordinates_are_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv('GEOAPIFY_API_KEY', 'secret-for-tests')
    g = gateway(tmp_path)
    with pytest.raises(ValueError, match='Latitude'):
        await g.geoapify_places(latitude=99, longitude=0)


@pytest.mark.asyncio
async def test_geoapify_transient_failure_uses_osm_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv('GEOAPIFY_API_KEY', 'secret-for-tests')
    g = gateway(tmp_path)
    calls = []
    async def geo(**kwargs):
        calls.append('geo')
        raise httpx.ReadTimeout('Timeout')
    async def osm(**kwargs):
        calls.append('osm')
        from aligo.providers import ProviderResult
        return ProviderResult({'results': []}, [])
    g.register('places','geoapify',geo,replace=True,required_env='GEOAPIFY_API_KEY')
    g.fallbacks['places'] = [('openstreetmap',osm,None)]
    out = await g.request('places', latitude=47, longitude=8)
    assert calls == ['geo','osm']
    assert out.data['results'] == []


@pytest.mark.asyncio
async def test_bad_api_key_does_not_silently_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv('GEOAPIFY_API_KEY', 'invalid-key')
    g = gateway(tmp_path)
    calls = []
    async def unauthorized(**kwargs):
        request = httpx.Request('GET', 'https://api.geoapify.com/v2/places')
        raise httpx.HTTPStatusError('unauthorized', request=request,
                                    response=httpx.Response(401, request=request))
    async def osm(**kwargs):
        calls.append('osm')
    g.register('places','geoapify',unauthorized,replace=True,required_env='GEOAPIFY_API_KEY')
    g.fallbacks['places'] = [('openstreetmap',osm,None)]
    with pytest.raises(ProviderUnavailable, match='HTTP 401'):
        await g.request('places', latitude=47, longitude=8)
    assert not calls
