"""Capability-based providers. No synthetic inventory, route metrics, or forecasts."""
from __future__ import annotations
import asyncio
import copy
import math
import time
from dataclasses import dataclass
import importlib
import json
import os
from pathlib import Path
from datetime import datetime, timezone
from typing import Any
import httpx
from core.contracts import Evidence, utc_now


class ProviderUnavailable(Exception):
    pass


@dataclass
class ProviderResult:
    data: dict
    evidence: list[Evidence]


class ProviderGateway:
    def __init__(self, settings):
        self.settings=settings
        self.adapters={}
        self.required_keys={}
        self.fallbacks={}
        self._lookup_cache: dict[tuple, tuple[float, ProviderResult]] = {}
        self._lookup_locks: dict[tuple, asyncio.Lock] = {}
        self.cache_hits = 0
        self.external_calls = 0
        self.register('geocode','openmeteo',self.geocode)
        self.register('weather','openmeteo',self.weather)
        # Geoapify provides an independent POI data path when explicitly configured.
        # Without credentials, retain the OpenStreetMap-only development mode.
        if os.getenv('GEOAPIFY_API_KEY', '').strip():
            self.register('places', 'geoapify', self.geoapify_places,
                          required_env='GEOAPIFY_API_KEY')
            self.register_fallback('places', 'openstreetmap', self.places)
        else:
            self.register('places', 'openstreetmap', self.places)
        self.register_fallback('places', 'overpass-private-coffee',
                               self.places_backup)
        self.register('walking_route','openrouteservice',self.walking_route)
        self.register('flights','duffel',self.flights)
        self.register('fx','frankfurter-reference',self.fx)
        from services.provider_expansion import register_extended_providers
        register_extended_providers(self)
        self._load_custom_adapters()

    def register(self, capability, name, function, *, replace=False, required_env=None):
        if capability in self.adapters and not replace:
            raise ValueError(f'Provider capability already registered: {capability}')
        self.adapters[capability]=(name,function)
        self.required_keys[capability]=required_env
        self._lookup_cache.clear()

    def register_fallback(self, capability, name, function, *, required_env=None):
        if capability not in self.adapters:
            raise ValueError(f'Primary provider is required for {capability}')
        if any(item[0] == name for item in self.fallbacks.get(capability, [])):
            raise ValueError(f'Duplicate fallback provider: {name}')
        self.fallbacks.setdefault(capability, []).append((name, function, required_env))
        self._lookup_cache.clear()

    def _eligible(self, capability, required_env=None):
        if capability == 'accommodation_offers':
            return bool(os.getenv('BOOKING_API_TOKEN', '').strip() and os.getenv('BOOKING_AFFILIATE_ID', '').strip())
        if required_env:
            return bool(os.getenv(required_env, '').strip())
        if capability == 'walking_route':
            return bool(self.settings.ors_token)
        if capability == 'flights':
            return bool(self.settings.duffel_token)
        return True

    def available(self, capability):
        if capability not in self.adapters or not self.settings.allow_online:
            return False
        if self._eligible(capability, self.required_keys.get(capability)):
            return True
        return any(bool(os.getenv(key, '')) if key else True for _, _, key in self.fallbacks.get(capability, []))

    def catalog(self):
        return [{'capability': cap, 'provider': name, 'configured': self.available(cap),
                 'requires_key': cap in ('walking_route', 'flights') or bool(self.required_keys.get(cap)),
                 'fallback_providers': [fallback[0] for fallback in self.fallbacks.get(cap, [])]}
                for cap, (name, _) in sorted(self.adapters.items())]

    @staticmethod
    def _cache_ttl(capability: str) -> int:
        setting = {'geocode': ('POREIAGO_GEOCODE_CACHE_TTL', 900),
                   'places': ('POREIAGO_PLACES_CACHE_TTL', 300)}.get(capability)
        if setting is None:
            return 0
        try:
            return max(0, min(int(os.getenv(setting[0], str(setting[1]))), 3600))
        except (TypeError, ValueError):
            return setting[1]

    async def request(self, capability, **kwargs):
        if capability not in self.adapters:
            raise ProviderUnavailable(f'No provider implements {capability}.')
        if not self.settings.allow_online:
            raise ProviderUnavailable('Online providers are disabled. Set POREIAGO_ALLOW_ONLINE=true.')
        ttl = self._cache_ttl(capability)
        key = None
        if ttl:
            try:
                # Sorted canonical arguments prevent an order-dependent cache key.
                key = (capability, json.dumps(kwargs, sort_keys=True, separators=(',', ':'),
                                              ensure_ascii=True, allow_nan=False))
            except (TypeError, ValueError):
                key = None
        if key is not None:
            cached = self._lookup_cache.get(key)
            if cached and cached[0] > time.monotonic():
                self.cache_hits += 1
                return copy.deepcopy(cached[1])
            # Deduplicate identical simultaneous requests within this instance.
            gate = self._lookup_locks.setdefault(key, [asyncio.Lock(), 0])
            gate[1] += 1
            try:
                async with gate[0]:
                    cached = self._lookup_cache.get(key)
                    if cached and cached[0] > time.monotonic():
                        self.cache_hits += 1
                        return copy.deepcopy(cached[1])
                    result = await self._request_uncached(capability, **kwargs)
                    if len(self._lookup_cache) >= 256:
                        self._lookup_cache.clear()
                    self._lookup_cache[key] = (time.monotonic() + ttl, copy.deepcopy(result))
                    return result
            finally:
                gate[1] -= 1
                if gate[1] == 0 and self._lookup_locks.get(key) is gate:
                    self._lookup_locks.pop(key, None)
        return await self._request_uncached(capability, **kwargs)

    async def _request_uncached(self, capability, **kwargs):
        main_name, main_func = self.adapters[capability]
        candidates = [(main_name, main_func, self.required_keys.get(capability))]
        candidates.extend(self.fallbacks.get(capability, []))
        errors = []
        for index, (name, func, key) in enumerate(candidates):
            if index == 0 and not self._eligible(capability, key):
                errors.append(f'{name}: credentials are not configured')
                continue
            if index > 0 and key and not os.getenv(key, ''):
                errors.append(f'{name}: credentials are not configured')
                continue
            try:
                self.external_calls += 1
                return await asyncio.wait_for(func(**kwargs), timeout=self.settings.request_timeout + 2)
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                errors.append(f'{name}: HTTP {status}')
                if status < 500 and status != 408:
                    break
            except (ProviderUnavailable, httpx.HTTPError, asyncio.TimeoutError) as exc:
                errors.append(f'{name}: {type(exc).__name__}')
        raise ProviderUnavailable(f'{capability} is unavailable (' + '; '.join(errors) + ').')

    def _load_custom_adapters(self):
        """Extensions are trusted executable code. Do not load user-submitted manifests."""
        location=Path(self.settings.provider_file)
        if not location.exists():return
        data=json.loads(location.read_text(encoding='utf-8'))
        for item in data.get('custom_adapters',[]):
            module_name,function_name=item['entrypoint'].split(':',1)
            function=getattr(importlib.import_module(module_name),function_name)
            if item.get('fallback', False):
                self.register_fallback(item['capability'], item['name'], function, required_env=item.get('required_env'))
            else:
                self.register(item['capability'],item['name'],function,replace=bool(item.get('replace',False)),required_env=item.get('required_env'))

    async def _get(self,url,params=None,headers=None):
        async with httpx.AsyncClient(timeout=self.settings.request_timeout,follow_redirects=False) as client:
            response=await client.get(url,params=params,headers=headers)
            response.raise_for_status()
            return response.json()

    async def geocode(self,place: str,**_):
        place=place.strip()[:120]
        if not place:raise ValueError('Specify a place name.')
        url='https://geocoding-api.open-meteo.com/v1/search'
        raw=await self._get(url,{'name':place,'count':5,'language':'en','format':'json'})
        choices=[{'name':r.get('name'),'country':r.get('country'),'admin1':r.get('admin1'),'latitude':r['latitude'],'longitude':r['longitude'],'timezone':r.get('timezone')} for r in raw.get('results',[]) if 'latitude' in r and 'longitude' in r]
        return ProviderResult({'results':choices},[Evidence(provider='open-meteo-geocoding',url='https://open-meteo.com/en/docs/geocoding-api',mode='live')])

    async def weather(self,latitude:float,longitude:float,forecast_days:int=3,**_):
        url='https://api.open-meteo.com/v1/forecast'
        days=max(1,min(int(forecast_days),14))
        data=await self._get(url,{'latitude':latitude,'longitude':longitude,'hourly':'temperature_2m,apparent_temperature,precipitation_probability,wind_speed_10m','timezone':'auto','forecast_days':days})
        return ProviderResult({'latitude':data.get('latitude'),'longitude':data.get('longitude'),'timezone':data.get('timezone'),'hourly':data.get('hourly',{}),'units':data.get('hourly_units',{})},[Evidence(provider='open-meteo-forecast',url='https://open-meteo.com/en/docs',mode='forecast')])

    async def geoapify_places(self, latitude: float, longitude: float,
                             kind: str = 'tourism', radius: int = 4500,
                             language: str | None = None, **_):
        """Find genuine places via Geoapify's category and radius API.

        Credentials are supplied through the GEOAPIFY_API_KEY environment
        variable. The API key is sent as a header, never returned in results.
        """
        lat = float(latitude)
        lon = float(longitude)
        if not math.isfinite(lat) or not -90 <= lat <= 90:
            raise ValueError('Latitude must be between -90 and 90.')
        if not math.isfinite(lon) or not -180 <= lon <= 180:
            raise ValueError('Longitude must be between -180 and 180.')
        key = os.getenv('GEOAPIFY_API_KEY', '').strip()
        if not key:
            raise ProviderUnavailable('Geoapify API credentials are not configured.')

        categories = {
            'tourism': 'tourism',
            'photo': 'tourism.attraction.viewpoint,tourism.attraction',
            'hotel': 'accommodation.hotel',
            'restaurant': 'catering.restaurant',
            'coast': 'natural.coastal,beach',
        }
        params = {
            'categories': categories.get(kind, 'tourism'),
            'filter': f'circle:{lon},{lat},{max(500, min(int(radius), 10000))}',
            'bias': f'proximity:{lon},{lat}',
            'limit': 30,
        }
        if language and isinstance(language, str) and len(language) == 2 and language.isalpha():
            params['lang'] = language.lower()
        raw = await self._get(
            'https://api.geoapify.com/v2/places', params=params,
            headers={'x-api-key': key, 'Accept': 'application/json'},
        )
        features = raw.get('features', []) if isinstance(raw, dict) else None
        if not isinstance(features, list):
            raise ProviderUnavailable('Geoapify returned an unexpected response format.')
        results = []
        for feature in features:
            if not isinstance(feature, dict):
                continue
            props = feature.get('properties') or {}
            geometry = feature.get('geometry') or {}
            if not isinstance(props, dict) or not isinstance(geometry, dict):
                continue
            coords = geometry.get('coordinates')
            if not isinstance(coords, list) or len(coords) < 2:
                continue
            name = props.get('name') or props.get('address_line1')
            if not name:
                continue
            try:
                x, y = float(coords[0]), float(coords[1])
            except (TypeError, ValueError):
                continue
            if not (math.isfinite(y) and -90 <= y <= 90 and
                    math.isfinite(x) and -180 <= x <= 180):
                continue
            tags = props.get('categories') or []
            results.append({
                'name': name,
                'latitude': y,
                'longitude': x,
                'category': tags[0] if isinstance(tags, list) and tags else 'place',
                'place_id': props.get('place_id'),
                'osm_type': (props.get('datasource') or {}).get('raw', {}).get('osm_type'),
                'osm_id': (props.get('datasource') or {}).get('raw', {}).get('osm_id'),
            })
        return ProviderResult(
            {'results': results[:30]},
            [Evidence(provider='geoapify-places',
                      url='https://apidocs.geoapify.com/docs/places/',
                      mode='retrieved')],
        )

    async def _overpass_places(self, endpoint: str, latitude: float, longitude: float,
                               kind: str = 'tourism', radius: int = 4500, **_):
        """Query a single public Overpass instance; return only actual map features."""
        if not math.isfinite(float(latitude)) or not -90 <= float(latitude) <= 90:
            raise ValueError('Latitude must be between -90 and 90.')
        if not math.isfinite(float(longitude)) or not -180 <= float(longitude) <= 180:
            raise ValueError('Longitude must be between -180 and 180.')
        filters = {
            'hotel': '["tourism"="hotel"]',
            'restaurant': '["amenity"="restaurant"]',
            'coast': '["natural"~"beach|coast|bay|cape"]',
        }
        osm_filter = filters.get(kind, '["tourism"~"attraction|viewpoint|museum|gallery"]')
        query = (
            f'[out:json][timeout:16];'
            f'(nwr(around:{max(500, min(int(radius), 10000))},'
            f'{float(latitude)},{float(longitude)}){osm_filter};);out center 30;'
        )
        async with httpx.AsyncClient(timeout=self.settings.request_timeout) as client:
            response = await client.post(
                endpoint,
                data={'data': query},
                headers={'User-Agent': 'PoreiaGo (OpenStreetMap place lookup)',
                         'Accept': 'application/json'},
            )
            response.raise_for_status()
            try:
                raw = response.json()
            except ValueError as exc:
                raise ProviderUnavailable('Overpass returned invalid JSON.') from exc
        if not isinstance(raw, dict) or not isinstance(raw.get('elements', []), list):
            raise ProviderUnavailable('Overpass returned an unexpected response format.')
        results = []
        for item in raw.get('elements', []):
            if not isinstance(item, dict):
                continue
            tags = item.get('tags') or {}
            center = item.get('center') or {}
            if not isinstance(tags, dict) or not isinstance(center, dict):
                continue
            lat = item.get('lat', center.get('lat'))
            lon = item.get('lon', center.get('lon'))
            if not tags.get('name') or lat is None or lon is None:
                continue
            results.append({
                'name': tags['name'], 'latitude': lat, 'longitude': lon,
                'category': tags.get('tourism', tags.get('natural', tags.get('amenity', 'place'))),
                'osm_type': item.get('type'), 'osm_id': item.get('id'),
            })
        return ProviderResult(
            {'results': results[:30]},
            [Evidence(provider='openstreetmap-overpass', url=endpoint, mode='retrieved')],
        )

    async def places(self, latitude: float, longitude: float, kind: str = 'tourism',
                     radius: int = 4500, **kwargs):
        return await self._overpass_places(
            os.getenv('POREIAGO_OVERPASS_PRIMARY_URL',
                      'https://overpass-api.de/api/interpreter'),
            latitude, longitude, kind, radius, **kwargs,
        )

    async def places_backup(self, latitude: float, longitude: float, kind: str = 'tourism',
                            radius: int = 4500, **kwargs):
        return await self._overpass_places(
            os.getenv('POREIAGO_OVERPASS_BACKUP_URL',
                      'https://overpass.private.coffee/api/interpreter'),
            latitude, longitude, kind, radius, **kwargs,
        )

    async def walking_route(self,waypoints:list,**_):
        if len(waypoints)<2 or len(waypoints)>12:raise ValueError('Walking directions require 2–12 coordinates.')
        coords=[[float(p['longitude']),float(p['latitude'])] for p in waypoints]
        async with httpx.AsyncClient(timeout=self.settings.request_timeout) as client:
            response=await client.post('https://api.openrouteservice.org/v2/directions/foot-walking/geojson',headers={'Authorization':self.settings.ors_token,'Content-Type':'application/json'},json={'coordinates':coords})
            response.raise_for_status();data=response.json()
        route=data['features'][0];summary=route['properties']['summary']
        return ProviderResult({'geometry':route['geometry'],'distance_m':summary['distance'],'duration_s':summary['duration'],'units':{'distance':'m','duration':'s'}},[Evidence(provider='openrouteservice',url='https://openrouteservice.org/',mode='live_route')])

    async def flights(self,origin:str,destination:str,departure_date:str,passengers:int=1,**_):
        if self.settings.duffel_mode not in {'test','live'}:raise ValueError('DUFFEL_MODE must be test or live.')
        from datetime import date
        date.fromisoformat(departure_date)
        if len(origin)!=3 or len(destination)!=3:raise ValueError('Flight search requires three-letter IATA airport codes.')
        payload={'data':{'slices':[{'origin':origin.upper(),'destination':destination.upper(),'departure_date':departure_date}],'passengers':[{'type':'adult'} for _ in range(max(1,min(int(passengers),8)))],'cabin_class':'economy'}}
        async with httpx.AsyncClient(timeout=self.settings.request_timeout) as client:
            response=await client.post('https://api.duffel.com/air/offer_requests',headers={'Authorization':f'Bearer {self.settings.duffel_token}','Duffel-Version':'v2','Content-Type':'application/json'},json=payload)
            response.raise_for_status();raw=response.json()['data']
        offers=[]
        for offer in raw.get('offers',[])[:12]:
            slices=offer.get('slices',[])
            offers.append({'id':offer.get('id'),'amount':offer.get('total_amount'),'currency':offer.get('total_currency'),'expires_at':offer.get('expires_at'),'slices':[{'duration':s.get('duration'),'segments':[{'origin':seg['origin'].get('iata_code'),'destination':seg['destination'].get('iata_code'),'departing_at':seg.get('departing_at'),'arriving_at':seg.get('arriving_at'),'airline':seg.get('marketing_carrier',{}).get('name')} for seg in s.get('segments',[])]} for s in slices]})
        return ProviderResult({'offers':offers,'mode':self.settings.duffel_mode,'offer_count':len(offers)},[Evidence(provider='duffel',url='https://duffel.com/docs/api/v2/offer-requests',mode=f'{self.settings.duffel_mode}_offer')])

    async def fx(self, amount: float, origin: str, destination: str, **_):
        """Convert using Frankfurter v2 reference rates without redirecting hosts.

        Frankfurter v2 returns one {base, quote, rate, date} object for a
        currency pair; these are reference rates, not guaranteed bank quotes.
        """
        import re

        base = str(origin).strip().upper()
        quote = str(destination).strip().upper()
        if not re.fullmatch(r"[A-Z]{3}", base) or not re.fullmatch(r"[A-Z]{3}", quote):
            raise ValueError("Use three-letter ISO currency codes.")
        value = float(amount)
        if not math.isfinite(value):
            raise ValueError("Amount must be finite.")
        if base == quote:
            return ProviderResult(
                {"amount": value, "converted": round(value, 2),
                 "from": base, "to": quote, "rate": 1.0, "date": None},
                [Evidence(provider="local-currency-identity", mode="local")],
            )
        response = await self._get(
            f"https://api.frankfurter.dev/v2/rate/{base.lower()}/{quote.lower()}"
        )
        if not isinstance(response, dict) or not isinstance(response.get("rate"), (int, float)):
            raise ProviderUnavailable("Frankfurter returned an invalid rate payload.")
        rate = float(response["rate"])
        if not math.isfinite(rate) or rate <= 0:
            raise ProviderUnavailable("Frankfurter returned a non-positive or non-finite rate.")
        return ProviderResult(
            {"amount": value, "converted": round(value * rate, 2),
             "from": base, "to": quote, "rate": rate, "date": response.get("date")},
            [Evidence(provider="frankfurter-reference", url="https://frankfurter.dev/", mode="daily_reference")],
        )
