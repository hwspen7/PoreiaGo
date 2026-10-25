"""Optional, source-backed travel-data adapters.

These are *data* capabilities, not additional LLM agents. Providers with
restricted inventories are only available with their official credentials.
All URLs are fixed by the application; user input cannot select a host.
"""
from __future__ import annotations

from datetime import date
import math
import os
import re
from urllib.parse import quote

import httpx
from core.contracts import Evidence
from services.providers import ProviderResult, ProviderUnavailable


def _key(name):
    return os.getenv(name, '').strip()


def _latlon(latitude, longitude):
    lat, lon = float(latitude), float(longitude)
    if not (math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError('Valid latitude and longitude coordinates are required.')
    return lat, lon


def _waypoints(waypoints):
    if not isinstance(waypoints, list) or not 2 <= len(waypoints) <= 12:
        raise ValueError('Provide between 2 and 12 coordinate waypoints.')
    return [_latlon(point['latitude'], point['longitude']) for point in waypoints]


def _iso_day(text):
    return date.fromisoformat(str(text)).isoformat()


def _features(raw):
    if not isinstance(raw, dict) or not isinstance(raw.get('features'), list):
        raise ProviderUnavailable('The provider returned an invalid GeoJSON response.')
    return raw['features']


class ExtendedTravelProviders:
    def __init__(self, gateway):
        self.gateway = gateway

    async def route(self, waypoints, mode='walk', language='en', **_):
        modes = {'walk', 'hike', 'drive', 'bicycle', 'mountain_bike', 'transit'}
        if mode not in modes:
            raise ValueError('Unsupported route travel mode.')
        coords = _waypoints(waypoints)
        if not _key('GEOAPIFY_API_KEY'):
            raise ProviderUnavailable('Geoapify credentials are not configured.')
        params = {'waypoints': '|'.join(f'{lat},{lon}' for lat, lon in coords),
                  'mode': mode, 'apiKey': _key('GEOAPIFY_API_KEY')}
        if isinstance(language, str) and re.fullmatch(r'[a-z]{2}(?:-[A-Za-z]{2})?', language):
            params['lang'] = language
        raw = await self.gateway._get('https://api.geoapify.com/v1/routing', params)
        features = _features(raw)
        if not features:
            raise ProviderUnavailable('No route was returned for the selected transport mode.')
        feat = features[0]
        prop = feat.get('properties') or {}
        distance, duration = prop.get('distance'), prop.get('time')
        if not isinstance(distance, (int, float)) or not isinstance(duration, (int, float)):
            raise ProviderUnavailable('The route provider returned no verified distance or time.')
        return ProviderResult({'geometry': feat.get('geometry'), 'distance_m': distance,
                               'duration_s': duration, 'travel_mode': mode,
                               'units': {'distance': 'm', 'duration': 's'},
                               'reliability_note': 'Public-transit routing depends on regional data coverage.' if mode == 'transit' else ''},
                              [Evidence(provider='geoapify-routing',
                                        url='https://apidocs.geoapify.com/docs/routing/', mode='live_route')])

    async def walk(self, waypoints, **kwargs):
        return await self.route(waypoints, mode='walk', **kwargs)

    async def drive(self, waypoints, **kwargs):
        return await self.route(waypoints, mode='drive', **kwargs)

    async def cycle(self, waypoints, **kwargs):
        return await self.route(waypoints, mode='bicycle', **kwargs)

    async def hiking(self, waypoints, **kwargs):
        return await self.route(waypoints, mode='hike', **kwargs)

    async def public_transit(self, waypoints, **kwargs):
        return await self.route(waypoints, mode='transit', **kwargs)

    async def matrix(self, sources, targets, mode='drive', **_):
        if mode not in {'drive', 'walk', 'bicycle'}:
            raise ValueError('Unsupported route-matrix mode.')
        if not isinstance(sources, list) or not isinstance(targets, list) or not sources or not targets or len(sources)*len(targets)>100:
            raise ValueError('Specify source and destination lists with at most 100 matrix cells.')
        if not _key('GEOAPIFY_API_KEY'):
            raise ProviderUnavailable('Geoapify credentials are not configured.')
        def locs(points):
            return [{'location': [lon, lat]} for lat, lon in (_latlon(p['latitude'], p['longitude']) for p in points)]
        payload = {'mode': mode, 'sources': locs(sources), 'targets': locs(targets)}
        async with httpx.AsyncClient(timeout=self.gateway.settings.request_timeout) as client:
            response = await client.post('https://api.geoapify.com/v1/routematrix',
                  params={'apiKey': _key('GEOAPIFY_API_KEY')}, json=payload)
            response.raise_for_status()
            raw = response.json()
        matrix = raw.get('sources_to_targets') if isinstance(raw, dict) else None
        if not isinstance(matrix, list):
            raise ProviderUnavailable('The routing service returned no distance matrix.')
        return ProviderResult({'sources_to_targets': matrix, 'mode': mode,
                               'source_count': len(sources), 'target_count': len(targets)},
                              [Evidence(provider='geoapify-route-matrix',
                                        url='https://apidocs.geoapify.com/docs/routing/route-matrix/',mode='live_route')])

    async def air_quality(self, latitude, longitude, forecast_days=2, **_):
        lat, lon = _latlon(latitude, longitude)
        days = max(1, min(int(forecast_days), 5))
        raw = await self.gateway._get('https://air-quality-api.open-meteo.com/v1/air-quality',
             {'latitude': lat, 'longitude': lon, 'hourly': 'us_aqi,pm2_5,pm10',
              'timezone': 'auto', 'forecast_days': days})
        hourly = raw.get('hourly') if isinstance(raw, dict) else None
        if not isinstance(hourly, dict) or not hourly.get('time'):
            raise ProviderUnavailable('Air quality forecast does not include hourly data.')
        return ProviderResult({'hourly': hourly, 'units': raw.get('hourly_units', {}),
                               'timezone': raw.get('timezone'), 'source_type': 'modeled_air_quality'},
                              [Evidence(provider='open-meteo-air-quality',
                                        url='https://open-meteo.com/en/docs/air-quality-api',mode='forecast')])

    async def place_details(self, place_id, **_):
        if not isinstance(place_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{10,160}', place_id):
            raise ValueError('A valid place_id from Geoapify search is required.')
        raw = await self.gateway._get('https://api.geoapify.com/v2/place-details',
                        {'id': place_id, 'apiKey': _key('GEOAPIFY_API_KEY')})
        items = _features(raw)
        if not items:
            return ProviderResult({'results': []}, [Evidence(provider='geoapify-place-details',
                url='https://apidocs.geoapify.com/docs/place-details/',mode='retrieved')])
        details = []
        for item in items:
            props = item.get('properties') or {}
            if props.get('feature_type', 'details') != 'details':
                continue
            details.append({'place_id': props.get('place_id'), 'name': props.get('name'),
                'formatted': props.get('formatted'), 'website': props.get('website'),
                'opening_hours': props.get('opening_hours'), 'categories': props.get('categories'),
                'geometry': item.get('geometry')})
        return ProviderResult({'results': details}, [Evidence(provider='geoapify-place-details',
            url='https://apidocs.geoapify.com/docs/place-details/',mode='retrieved')])

    async def dining(self, latitude, longitude, radius=3000, language=None, **_):
        result = await self.gateway.request('places', latitude=latitude, longitude=longitude,
                 kind='restaurant', radius=radius, language=language)
        return ProviderResult({'results': result.data.get('results', []),
                               'reservation_availability_checked': False}, result.evidence)

    async def attractions(self, latitude, longitude, radius=4500, language=None, **_):
        result = await self.gateway.request('places', latitude=latitude, longitude=longitude,
                  kind='tourism', radius=radius, language=language)
        return ProviderResult({'results': result.data.get('results', []),
                               'ticket_availability_checked': False}, result.evidence)

    async def events(self, keyword=None, city=None, country_code=None,
                     start_date=None, latitude=None, longitude=None, radius_km=30, **_):
        key = _key('TICKETMASTER_API_KEY')
        if not key:
            raise ProviderUnavailable('Ticketmaster credentials are not configured.')
        params = {'apikey': key, 'size': 20}
        if keyword:params['keyword'] = str(keyword)[:100]
        if city:params['city'] = str(city)[:100]
        if country_code:
            country_code = str(country_code).upper()
            if not re.fullmatch(r'[A-Z]{2}', country_code):raise ValueError('Invalid country code.')
            params['countryCode'] = country_code
        if latitude is not None and longitude is not None:
            lat, lon = _latlon(latitude, longitude)
            params.update({'latlong': f'{lat},{lon}', 'radius': min(max(int(radius_km),1),100), 'unit':'km'})
        if start_date:
            params['startDateTime'] = f'{_iso_day(start_date)}T00:00:00Z'
        raw = await self.gateway._get('https://app.ticketmaster.com/discovery/v2/events.json', params)
        events = (raw.get('_embedded') or {}).get('events', []) if isinstance(raw,dict) else None
        if not isinstance(events,list):raise ProviderUnavailable('Invalid event search response.')
        results=[]
        for event in events:
            venue=((event.get('_embedded') or {}).get('venues') or [{}])[0]
            results.append({'id': event.get('id'), 'name': event.get('name'),
                'url': event.get('url'), 'date': (event.get('dates') or {}).get('start',{}).get('localDate'),
                'venue': venue.get('name'), 'city': (venue.get('city') or {}).get('name')})
        return ProviderResult({'results':results,'coverage':'ticketmaster_inventory_only'},
            [Evidence(provider='ticketmaster-discovery',
                      url='https://developer.ticketmaster.com/products-and-docs/apis/discovery-api/v2/',mode='live_inventory')])

    async def accommodation_offers(self, checkin, checkout, adults=2, rooms=1,
                                   booker_country='us', city_id=None, country_code=None,
                                   platform='desktop', **_):
        token,affiliate = _key('BOOKING_API_TOKEN'),_key('BOOKING_AFFILIATE_ID')
        if not token or not affiliate:
            raise ProviderUnavailable('Booking.com Demand API partner credentials are required.')
        start,end = _iso_day(checkin),_iso_day(checkout)
        if end<=start:raise ValueError('Checkout must be after checkin.')
        if platform not in {'desktop','mobile','tablet'}:raise ValueError('Unsupported booking platform.')
        if not re.fullmatch(r'[A-Za-z]{2}',str(booker_country)):
            raise ValueError('A two-letter booker country code is required.')
        payload={'booker':{'country':str(booker_country).lower(),'platform':platform},
                 'checkin':start,'checkout':end,
                 'guests':{'number_of_adults':max(1,min(int(adults),16)),
                           'number_of_rooms':max(1,min(int(rooms),8))}}
        if city_id is not None:payload['city'] = int(city_id)
        elif country_code and re.fullmatch(r'[A-Za-z]{2}',str(country_code)):
            payload['country'] = str(country_code).lower()
        else:raise ValueError('Provide a Booking.com city ID or a two-letter destination country.')
        host = ('https://demandapi.booking.com' if os.getenv('BOOKING_MODE','sandbox') == 'live'
                else 'https://demandapi-sandbox.booking.com')
        async with httpx.AsyncClient(timeout=self.gateway.settings.request_timeout) as client:
            response=await client.post(host+'/3.1/accommodations/search',json=payload,
                   headers={'Authorization':f'Bearer {token}', 'X-Affiliate-Id':affiliate,
                            'Content-Type':'application/json'})
            response.raise_for_status()
            raw=response.json()
        offers=raw.get('data',[]) if isinstance(raw,dict) else None
        if not isinstance(offers,list):raise ProviderUnavailable('Booking.com returned an invalid search response.')
        normalized=[]
        for item in offers[:25]:
            price=item.get('price') or {}
            normalized.append({'id':item.get('id'),'name':item.get('name'),
                               'price':price,'currency':item.get('currency'),'url':item.get('url'),
                               'products':item.get('products'),
                               'checkin':start,'checkout':end})
        return ProviderResult({'results':normalized, 'inventory_mode':os.getenv('BOOKING_MODE','sandbox'),
                               'room_availability_checked':True},
                              [Evidence(provider='booking-demand',
                              url='https://developers.booking.com/demand/docs/accommodations/about-accommodation',
                              mode='sandbox_inventory' if os.getenv('BOOKING_MODE','sandbox')!='live' else 'live_inventory')])

    @staticmethod
    def _validate_country_query(country: str) -> str:
        if not isinstance(country, str) or not country.strip() or len(country) > 90:
            raise ValueError('Provide a country name or ISO country code.')
        return country.strip()

    async def worldbank_country(self, country: str, **_):
        """No-key reference lookup. Country names and codes are not translated.

        World Bank does not supply language, currency or time-zone data on this
        endpoint. Return null for those fields instead of inventing them.
        """
        query = self._validate_country_query(country)
        raw = await self.gateway._get(
            'https://api.worldbank.org/v2/country',
            {'format': 'json', 'per_page': 400},
        )
        if (not isinstance(raw, list) or len(raw) != 2 or
                not isinstance(raw[1], list)):
            raise ProviderUnavailable('World Bank returned an invalid country-list response.')
        query_folded = query.casefold()
        results = []
        for item in raw[1]:
            if not isinstance(item, dict):
                continue
            name = item.get('name')
            code = item.get('iso2Code')
            alpha3 = item.get('id')
            region = item.get('region') or {}
            if not isinstance(region, dict) or region.get('id') == 'NA':
                continue  # Skip non-country aggregates, including regions and income groups.
            if not any(isinstance(value, str) and value.casefold() == query_folded
                       for value in (name, code, alpha3)):
                continue
            capital = item.get('capitalCity')
            results.append({
                'name': name,
                'country_code': code,
                'capital': [capital] if capital else [],
                'currencies': None,
                'languages': None,
                'timezones': None,
                'region': region.get('value'),
                'source_coverage': 'basic_country_reference',
            })
        return ProviderResult(
            {'results': results[:10], 'visa_rules_verified': False,
             'entry_rules_verified': False, 'source_coverage': 'basic_country_reference',
             'unsupported_fields': ['currencies', 'languages', 'timezones']},
            [Evidence(provider='worldbank-country-reference',
                      url='https://datahelpdesk.worldbank.org/knowledgebase/articles/898590-country-api-queries',
                      mode='reference')],
        )

    async def restcountries_v5(self, country: str, **_):
        """Richer multilingual reference using the authenticated v5 Countries API."""
        query = self._validate_country_query(country)
        key = _key('RESTCOUNTRIES_API_KEY')
        if not key:
            raise ProviderUnavailable('REST Countries V5 credentials are not configured.')
        raw = await self.gateway._get(
            'https://api.restcountries.com/countries/v5',
            {'q': query, 'limit': 10,
             'response_fields': 'names.common,codes.alpha_2,capitals,currencies,languages,timezones,region'},
            headers={'Authorization': f'Bearer {key}', 'Accept': 'application/json'},
        )
        objects = (raw.get('data') or {}).get('objects') if isinstance(raw, dict) else None
        if not isinstance(objects, list):
            raise ProviderUnavailable('REST Countries V5 returned an invalid response.')
        results = []
        for item in objects[:10]:
            if not isinstance(item, dict):
                continue
            capitals = item.get('capitals') or []
            if not isinstance(capitals, list):
                capitals = []
            normalized_capitals = [part.get('name') for part in capitals
                                   if isinstance(part, dict) and part.get('name')]
            results.append({
                'name': (item.get('names') or {}).get('common'),
                'country_code': (item.get('codes') or {}).get('alpha_2'),
                'capital': normalized_capitals,
                'currencies': item.get('currencies'),
                'languages': item.get('languages'),
                'timezones': item.get('timezones'),
                'region': item.get('region'),
                'source_coverage': 'extended_country_reference',
            })
        return ProviderResult(
            {'results': results, 'visa_rules_verified': False,
             'entry_rules_verified': False, 'source_coverage': 'extended_country_reference'},
            [Evidence(provider='restcountries-v5',
                      url='https://restcountries.com/docs/countries', mode='reference')],
        )


def register_extended_providers(gateway):
    """One registration site; no hardcoded destinations or travel preferences."""
    extra=ExtendedTravelProviders(gateway)
    providers=(
        ('air_quality','openmeteo-air-quality', extra.air_quality, None),
        ('place_details','geoapify-place-details',extra.place_details,'GEOAPIFY_API_KEY'),
        ('dining','mapped-dining',extra.dining,None),
        ('attractions','mapped-attractions',extra.attractions,None),
        ('route_matrix','geoapify-route-matrix',extra.matrix,'GEOAPIFY_API_KEY'),
        ('driving_route','geoapify-drive',extra.drive,'GEOAPIFY_API_KEY'),
        ('cycling_route','geoapify-cycle',extra.cycle,'GEOAPIFY_API_KEY'),
        ('hiking_route','geoapify-hike',extra.hiking,'GEOAPIFY_API_KEY'),
        ('transit_route','geoapify-transit',extra.public_transit,'GEOAPIFY_API_KEY'),
        ('events','ticketmaster',extra.events,'TICKETMASTER_API_KEY'),
        ('accommodation_offers','booking-demand',extra.accommodation_offers,'BOOKING_API_TOKEN'),
    )
    for cap,name,func,key in providers:
        gateway.register(cap,name,func,required_env=key)
    # REST Countries v1-v4 are deprecated; v5 requires an API key.
    # Preserve a no-key, limited-data World Bank reference provider.
    if _key('RESTCOUNTRIES_API_KEY'):
        gateway.register('country_context', 'restcountries-v5', extra.restcountries_v5,
                         required_env='RESTCOUNTRIES_API_KEY')
        gateway.register_fallback('country_context', 'worldbank-country-reference',
                                  extra.worldbank_country)
    else:
        gateway.register('country_context', 'worldbank-country-reference',
                         extra.worldbank_country)
    # Geoapify is a genuinely independent routing implementation when ORS is not configured.
    gateway.register_fallback('walking_route','geoapify-walk',extra.walk,
                              required_env='GEOAPIFY_API_KEY')
