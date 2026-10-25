"""Reusable low-level invariants for independently implemented travel skills."""
from __future__ import annotations
import math
import re
from core.contracts import SkillOutput


def reply(text, *, status='ok', data=None, sources=None, limitations=None, error_code=None):
    return SkillOutput(status=status, text=text, data=data or {}, sources=sources or [],
                       limitations=limitations or [], error_code=error_code)


def slug(value):
    return ' '.join(re.findall(r'[^\W_]+', str(value or '').casefold()))


def coordinates(args):
    if args.get('latitude') is None or args.get('longitude') is None:
        return None
    a, b = float(args['latitude']), float(args['longitude'])
    if not math.isfinite(a) or not math.isfinite(b) or not (-90 <= a <= 90 and -180 <= b <= 180):
        raise ValueError('Coordinates outside valid latitude/longitude range.')
    return a, b


def place_record(row):
    return {key: row[key] for key in ('name','latitude','longitude','category','place_id',
                                    'osm_type','osm_id','source_url','address','area','neighborhood') if key in row}


def finite_price(value):
    if isinstance(value, bool) or not isinstance(value, (int,float)) or not math.isfinite(value) or value < 0:
        return None
    return round(float(value), 2)


def parse_time(text):
    text = str(text or '')
    if not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', text):
        return None
    h, m = map(int, text.split(':'))
    return 60*h + m
