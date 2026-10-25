"""Mocked country reference integrations; no external API traffic or credentials."""
from dataclasses import replace
import httpx
import pytest
from core.config import settings
from services.providers import ProviderGateway


def gateway(monkeypatch, key=None):
    monkeypatch.delenv('RESTCOUNTRIES_API_KEY', raising=False)
    if key is not None:
        monkeypatch.setenv('RESTCOUNTRIES_API_KEY', key)
    return ProviderGateway(replace(settings, allow_online=True))


def worldbank_data():
    return [
        {'page': 1, 'pages': 1, 'total': 3},
        [
            {'id': 'FRA', 'iso2Code': 'FR', 'name': 'France',
             'capitalCity': 'Paris', 'region': {'id': 'ECS', 'value': 'Europe & Central Asia'}},
            {'id': 'WLD', 'iso2Code': '1W', 'name': 'World',
             'capitalCity': '', 'region': {'id': 'NA', 'value': 'Aggregates'}},
            {'id': 'BRA', 'iso2Code': 'BR', 'name': 'Brazil',
             'capitalCity': 'Brasilia', 'region': {'id': 'LCN', 'value': 'Latin America & Caribbean'}},
        ],
    ]


@pytest.mark.asyncio
async def test_worldbank_country_lookup_preserves_explicit_unknowns(monkeypatch):
    g = gateway(monkeypatch)
    assert next(v for v in g.catalog() if v['capability'] == 'country_context')['provider'] == 'worldbank-country-reference'

    async def fake(url, params=None, headers=None):
        assert url == 'https://api.worldbank.org/v2/country'
        assert params == {'format': 'json', 'per_page': 400}
        assert headers is None
        return worldbank_data()

    monkeypatch.setattr(g, '_get', fake)
    result = await g.request('country_context', country='FR')
    item = result.data['results'][0]
    assert item['name'] == 'France' and item['capital'] == ['Paris']
    assert item['currencies'] is None and item['languages'] is None and item['timezones'] is None
    assert result.data['visa_rules_verified'] is False
    assert result.evidence[0].provider == 'worldbank-country-reference'
    assert (await g.request('country_context', country='FRA')).data['results'][0]['country_code'] == 'FR'


@pytest.mark.asyncio
async def test_worldbank_rejects_aggregates_and_unknown_names(monkeypatch):
    g = gateway(monkeypatch)
    async def fake(*args, **kwargs):
        return worldbank_data()
    monkeypatch.setattr(g, '_get', fake)
    assert not (await g.request('country_context', country='World')).data['results']
    assert not (await g.request('country_context', country='Unknown Country')).data['results']


@pytest.mark.asyncio
async def test_restcountries_v5_auth_and_shape(monkeypatch):
    g = gateway(monkeypatch, key='example-token')
    provider = next(v for v in g.catalog() if v['capability'] == 'country_context')
    assert provider['provider'] == 'restcountries-v5'
    assert 'worldbank-country-reference' in provider['fallback_providers']

    async def fake(url, params=None, headers=None):
        assert url == 'https://api.restcountries.com/countries/v5'
        assert params['q'] == 'France'
        assert 'example-token' not in url and 'example-token' not in str(params)
        assert headers['Authorization'] == 'Bearer example-token'
        return {'data': {'objects': [{'names': {'common': 'France'},
                'codes': {'alpha_2': 'FR'}, 'capitals': [{'name':'Paris'}],
                'currencies': {'EUR': {'name': 'Euro'}},
                'languages': [{'tag': 'fr', 'name': 'French'}],
                'timezones': ['UTC+01:00'], 'region': 'Europe'}]}}

    monkeypatch.setattr(g, '_get', fake)
    result = await g.request('country_context', country='France')
    item = result.data['results'][0]
    assert item['country_code'] == 'FR'
    assert item['capital'] == ['Paris']
    assert 'EUR' in item['currencies']
    assert result.data['entry_rules_verified'] is False
    assert result.evidence[0].provider == 'restcountries-v5'


@pytest.mark.asyncio
async def test_worldbank_fallback_after_temporary_v5_failure(monkeypatch):
    g = gateway(monkeypatch, key='example-token')
    calls = []
    async def fake(url, params=None, headers=None):
        calls.append(url)
        if 'restcountries.com' in url:
            request = httpx.Request('GET', url)
            response = httpx.Response(503, request=request)
            raise httpx.HTTPStatusError('Temporary unavailability', request=request, response=response)
        assert headers is None
        return worldbank_data()
    monkeypatch.setattr(g, '_get', fake)
    result = await g.request('country_context', country='France')
    assert len(calls) == 2
    assert result.evidence[0].provider == 'worldbank-country-reference'


@pytest.mark.asyncio
async def test_reject_bad_country_queries_without_network(monkeypatch):
    g = gateway(monkeypatch)
    async def fail(*args, **kwargs):
        raise AssertionError('Bad inputs must fail before any HTTP request')
    monkeypatch.setattr(g, '_get', fail)
    with pytest.raises(ValueError):
        await g.request('country_context', country='')
    with pytest.raises(ValueError):
        await g.request('country_context', country='X'*91)
