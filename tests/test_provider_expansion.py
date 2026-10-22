"""Mock external data: no real keys, quota consumption, or network activity."""
import os
import pytest
from dataclasses import replace
from aligo.config import settings
from aligo.providers import ProviderGateway, ProviderUnavailable


def gateway(monkeypatch, **keys):
    for k in ('GEOAPIFY_API_KEY','TICKETMASTER_API_KEY','BOOKING_API_TOKEN','BOOKING_AFFILIATE_ID','RESTCOUNTRIES_API_KEY'):
        monkeypatch.delenv(k, raising=False)
    for key, value in keys.items():
        monkeypatch.setenv(key, value)
    return ProviderGateway(replace(settings, allow_online=True))


def test_registry_and_credentials(monkeypatch):
    g=gateway(monkeypatch)
    catalog={v['capability']:v for v in g.catalog()}
    assert len(catalog) >= 18
    assert catalog['air_quality']['configured']
    assert catalog['country_context']['configured']
    for name in ('driving_route','transit_route','place_details','events','accommodation_offers'):
        assert not catalog[name]['configured']
    g=gateway(monkeypatch, GEOAPIFY_API_KEY='test',BOOKING_API_TOKEN='test')
    assert g.available('walking_route')
    assert not g.available('accommodation_offers')
    monkeypatch.setenv('BOOKING_AFFILIATE_ID','test')
    assert g.available('accommodation_offers')


@pytest.mark.asyncio
async def test_route_geojson_and_coordinate_order(monkeypatch):
    g=gateway(monkeypatch,GEOAPIFY_API_KEY='dummy')
    async def fake(url,params=None,headers=None):
        assert url.endswith('/v1/routing')
        assert params['waypoints']=='48.1,2.2|48.2,2.3'
        assert params['mode']=='walk'
        return {'features':[{'geometry':{'type':'MultiLineString','coordinates':[]},
                            'properties':{'distance':3650,'time':2350}}]}
    monkeypatch.setattr(g,'_get',fake)
    res=await g.request('walking_route',waypoints=[{'latitude':48.1,'longitude':2.2},
                 {'latitude':48.2,'longitude':2.3}])
    # Walking fallback selects Geoapify if ORS is unconfigured.
    assert res.data['distance_m']==3650
    assert res.evidence[0].provider=='geoapify-routing'


@pytest.mark.asyncio
async def test_weather_and_country_context(monkeypatch):
    g=gateway(monkeypatch)
    async def fake(url,params=None,headers=None):
        if 'air-quality' in url:
            return {'hourly':{'time':['2026-10-18T10:00'],'us_aqi':[31]},'hourly_units':{},'timezone':'Europe/Paris'}
        assert 'api.worldbank.org/v2/country' in url
        assert params['format'] == 'json'
        return [{'page':1}, [{'id':'FRA','iso2Code':'FR','name':'France',
                 'capitalCity':'Paris','region':{'id':'ECS','value':'Europe & Central Asia'}}]]
    monkeypatch.setattr(g,'_get',fake)
    aq=await g.request('air_quality',latitude=48.8,longitude=2.3)
    assert aq.data['source_type']=='modeled_air_quality'
    cc=await g.request('country_context',country='France')
    assert cc.data['results'][0]['country_code']=='FR'
    assert cc.data['visa_rules_verified'] is False


@pytest.mark.asyncio
async def test_place_details_and_dining(monkeypatch):
    g=gateway(monkeypatch,GEOAPIFY_API_KEY='secret')
    async def fake(url,params=None,headers=None):
        assert 'secret' not in url
        if 'place-details' in url:
            return {'features':[{'properties':{'place_id':'a'*30,'feature_type':'details','name':'Museum','opening_hours':'Mo-Fr 09:00-17:00'},'geometry':{}}]}
        return {'features':[{'properties':{'name':'Restaurant','categories':['catering.restaurant'],'place_id':'f'},
                 'geometry':{'coordinates':[2.35,48.85]}}]}
    monkeypatch.setattr(g,'_get',fake)
    r=await g.request('place_details',place_id='a'*30)
    assert r.data['results'][0]['opening_hours']=='Mo-Fr 09:00-17:00'
    d=await g.request('dining',latitude=48.85,longitude=2.35)
    assert d.data['results'][0]['name']=='Restaurant'
    assert not d.data['reservation_availability_checked']


@pytest.mark.asyncio
async def test_event_search_mapping(monkeypatch):
    g=gateway(monkeypatch,TICKETMASTER_API_KEY='secret')
    async def fake(url,params=None,headers=None):
        assert url.endswith('events.json') and params['apikey']=='secret'
        return {'_embedded':{'events':[{'id':'e1','name':'Concert','url':'https://tickets.example/1',
                'dates':{'start':{'localDate':'2026-11-12'}},
                '_embedded':{'venues':[{'name':'Hall','city':{'name':'Paris'}}]}}]}}
    monkeypatch.setattr(g,'_get',fake)
    result=await g.request('events',city='Paris',start_date='2026-11-12')
    assert result.data['results'][0]['date']=='2026-11-12'
    assert result.data['results'][0]['venue']=='Hall'


@pytest.mark.asyncio
async def test_matrix_payload(monkeypatch):
    from aligo import provider_expansion as mod
    g=gateway(monkeypatch,GEOAPIFY_API_KEY='secret')
    captured={}
    class Response:
        def raise_for_status(self):pass
        def json(self):return {'sources_to_targets':[[{'distance': 34, 'time': 5}]]}
    class Client:
        def __init__(self,**kwargs):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def post(self,url,**kwargs):
            captured.update(kwargs)
            assert url.endswith('/v1/routematrix')
            return Response()
    monkeypatch.setattr(mod.httpx,'AsyncClient',Client)
    point={'latitude':45.2,'longitude':9.1}
    result=await g.request('route_matrix',sources=[point],targets=[point])
    assert captured['json']['sources'][0]['location']==[9.1,45.2]
    assert result.data['source_count']==1


@pytest.mark.asyncio
async def test_booking_partner_request(monkeypatch):
    from aligo import provider_expansion as mod
    g=gateway(monkeypatch,BOOKING_API_TOKEN='secret',BOOKING_AFFILIATE_ID='345')
    captured={}
    class Response:
        def raise_for_status(self):pass
        def json(self):return {'data':[{'id':2,'currency':'EUR','price':{'total':122.5},'url':'https://booking.example'}]}
    class Client:
        def __init__(self,**kwargs):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def post(self,url,**kwargs):
            assert url.startswith('https://demandapi-sandbox.booking.com')
            captured.update(kwargs)
            return Response()
    monkeypatch.setattr(mod.httpx,'AsyncClient',Client)
    result=await g.request('accommodation_offers',city_id=-2140479,checkin='2026-11-12',checkout='2026-11-13')
    assert captured['json']['city']==-2140479
    assert captured['headers']['X-Affiliate-Id']=='345'
    assert result.data['results'][0]['currency']=='EUR'
    assert result.data['inventory_mode']=='sandbox'


@pytest.mark.asyncio
async def test_reject_invalid_without_network(monkeypatch):
    g=gateway(monkeypatch,GEOAPIFY_API_KEY='secret',TICKETMASTER_API_KEY='secret')
    with pytest.raises(ValueError):
        await g.request('place_details',place_id='../etc/passwd')
    with pytest.raises(ValueError):
        await g.request('driving_route',waypoints=[{'latitude':999,'longitude':0},{'latitude':0,'longitude':0}])
    with pytest.raises(ValueError):
        await g.request('events',country_code='Germany')
