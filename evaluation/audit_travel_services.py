"""Audit the extended PoreiaGo travel providers against real services.

Run as a module from the project root:
    python -m evaluation.audit_travel_services
    python -m evaluation.audit_travel_services --with-routing --with-partners

Opt-in flags are required for extra requests that can consume paid quotas.
No credentials, full request URLs, or private user information are logged.
"""
from __future__ import annotations
import argparse
import asyncio
from datetime import date, timedelta
from time import monotonic
from core.config import settings
from services.providers import ProviderGateway, ProviderUnavailable


def samples(lat, lon):
    points=[{'latitude':lat,'longitude':lon},
            {'latitude':lat+0.002,'longitude':lon+0.002}]
    return {
      'geocode': {'place':'Paris'},
      'weather': {'latitude':lat,'longitude':lon,'forecast_days':1},
      'air_quality': {'latitude':lat,'longitude':lon,'forecast_days':1},
      'places': {'latitude':lat,'longitude':lon,'radius':2000},
      'dining': {'latitude':lat,'longitude':lon,'radius':1500},
      'attractions': {'latitude':lat,'longitude':lon,'radius':2000},
      'fx': {'amount':10,'origin':'USD','destination':'EUR'},
      'country_context': {'country':'France'},
      'walking_route': {'waypoints':points},
      'driving_route': {'waypoints':points},
      'cycling_route': {'waypoints':points},
      'hiking_route': {'waypoints':points},
      'transit_route': {'waypoints':points},
      'route_matrix': {'sources':[points[0]], 'targets':[points[1]]},
      'events': {'city':'Paris','country_code':'FR'},
      'accommodation_offers': {
          'city_id':-2140479, 'checkin':(date.today()+timedelta(days=30)).isoformat(),
          'checkout':(date.today()+timedelta(days=32)).isoformat(),
          'booker_country':'gb'},
      'flights': {'origin':'LHR','destination':'CDG',
                  'departure_date':(date.today()+timedelta(days=30)).isoformat()},
    }


def validate(cap, payload):
    if not isinstance(payload,dict): return 'FAILED', 'Invalid payload type'
    if cap in {'geocode','places','dining','attractions','events','accommodation_offers','country_context','place_details'}:
        values=payload.get('results')
        if not isinstance(values,list):return 'FAILED','Missing results array'
        return ('PASSED' if values else 'EMPTY'),f'{len(values)} result(s)'
    if cap in {'weather','air_quality'}:
        hourly=payload.get('hourly')
        return ('PASSED','Hourly data returned') if isinstance(hourly,dict) and hourly.get('time') else ('FAILED','Hourly data missing')
    if cap=='fx':return ('PASSED','Reference rate returned') if isinstance(payload.get('rate'),(int,float)) else ('FAILED','Rate missing')
    if cap in {'walking_route','driving_route','cycling_route','hiking_route','transit_route'}:
        return ('PASSED','Route and distance returned') if isinstance(payload.get('distance_m'),(int,float)) else ('FAILED','No route distance')
    if cap=='route_matrix':return ('PASSED','Route matrix returned') if isinstance(payload.get('sources_to_targets'),list) else ('FAILED','No distance matrix')
    if cap=='flights':return ('PASSED','Offer list returned') if isinstance(payload.get('offers'),list) else ('FAILED','No offer list')
    return 'FAILED','Unknown capability'


async def run(args):
    provider=ProviderGateway(settings)
    known={x['capability']:x for x in provider.catalog()}
    tasks=samples(args.latitude,args.longitude)
    # Place details needs a genuine Geoapify place identifier, which should
    # come from a previous places search; skip it in the generic smoke audit.
    selected = [x.strip() for x in args.only.split(',') if x.strip()] if args.only else list(tasks)
    unknown=set(selected)-set(known)
    if unknown:
        print('Unknown capabilities:',', '.join(sorted(unknown)))
        return 2
    print('PoreiaGo Extended Provider Audit — real requests, not unit tests')
    print('Configured online:',settings.allow_online)
    print('Live requests can consume external provider quotas.\n')
    skip_routes={'walking_route','driving_route','cycling_route','hiking_route','transit_route','route_matrix'}
    skip_partners={'flights','accommodation_offers','events'}
    statuses=[]
    for cap in selected:
        item=known[cap]
        if cap not in tasks:
            status,detail='SKIPPED','A prior provider result is needed as input'
        elif not item['configured']:
            status,detail='SKIPPED','Provider is disabled or missing credentials'
        elif cap in skip_routes and not args.with_routing:
            status,detail='SKIPPED','Use --with-routing to authorize route API requests'
        elif cap in skip_partners and not args.with_partners:
            status,detail='SKIPPED','Use --with-partners to authorize partner API requests'
        else:
            t0=monotonic()
            try:
                data=(await provider.request(cap,**tasks[cap])).data
                status,detail=validate(cap,data)
            except ProviderUnavailable as e:
                status,detail='FAILED',str(e)[:180]
            except Exception as e:
                status,detail='FAILED',type(e).__name__
            detail+=f' ({monotonic()-t0:.2f}s)'
        statuses.append(status)
        print(f'[{status:7}] {cap:22s} {detail}')
    print('\nSummary:', ', '.join(f'{s}={statuses.count(s)}' for s in ('PASSED','FAILED','EMPTY','SKIPPED')))
    return 1 if 'FAILED' in statuses else 0


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--only',default='',help='Comma-separated capabilities to test')
    parser.add_argument('--latitude',type=float,default=48.8566)
    parser.add_argument('--longitude',type=float,default=2.3522)
    parser.add_argument('--with-routing',action='store_true',help='Consent to route and matrix API quota usage')
    parser.add_argument('--with-partners',action='store_true',help='Consent to events, hotel and flight API quota usage')
    raise SystemExit(asyncio.run(run(parser.parse_args())))

if __name__=='__main__':main()
