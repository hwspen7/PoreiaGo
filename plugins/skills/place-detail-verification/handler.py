"""Resolve candidate venue identity conservatively from IDs, aliases and provider details."""
from __future__ import annotations

from aligo.providers import ProviderUnavailable
from aligo.skill_support import place_record, reply, slug


def _same_geography(row, city, country):
    if not city and not country:
        return True
    city_value = slug(row.get('city'))
    country_value = slug(row.get('country'))
    address = slug(row.get('address'))
    if city and ((city_value and city_value != city) or (not city_value and address and city not in address)):
        return False
    if country and ((country_value and country_value != country) or
                    (not country_value and address and country not in address)):
        return False
    return True


async def run(ctx, args):
    target = slug(args.get('place') or args.get('name'))
    place_id = str(args.get('place_id') or '').strip()
    if not target and not place_id:
        return reply('Supply a place name or stable provider place ID.', status='partial')
    source = args.get('candidates', [])
    if not isinstance(source, list):
        return reply('Candidates must be a list.', status='partial')
    city, country = slug(args.get('city')), slug(args.get('country'))
    matches, seen, rejected = [], set(), 0
    for row in source[:150]:
        if not isinstance(row, dict):
            continue
        aliases = [row.get('name')] + (row.get('aliases') if isinstance(row.get('aliases'), list) else [])
        name_match = target and target in {slug(name) for name in aliases}
        id_match = bool(place_id and str(row.get('place_id') or '') == place_id)
        if place_id and not id_match:
            continue  # Never silently swap a requested ID for a same-name competitor.
        if not place_id and not name_match:
            continue
        if not _same_geography(row, city, country):
            rejected += 1
            continue
        record = place_record(row)
        identity = str(row.get('place_id') or row.get('osm_id') or
                       f"{slug(row.get('name'))}|{slug(row.get('address'))}")
        if identity in seen:
            continue
        seen.add(identity)
        matches.append(record)
    details, sources = [], []
    if place_id and getattr(ctx, 'providers', None) is not None:
        try:
            fetched = await ctx.providers.request('place_details', place_id=place_id)
            sources = fetched.evidence
            for item in (fetched.data.get('results') or [])[:10]:
                if (isinstance(item, dict) and str(item.get('place_id') or '') == place_id
                        and _same_geography(item, city, country)):
                    details.append(place_record(item))
        except (ProviderUnavailable, ValueError, TypeError, KeyError):
            pass
    # A unique candidate with no globally identified place remains a candidate,
    # not a factual assertion. Conflicting locations require human selection.
    ambiguous = len(matches) > 1 or len(details) > 1
    match = matches[0] if len(matches) == 1 and not ambiguous else None
    if not match and len(details) == 1:
        match = details[0]
    return reply('Candidate identity and requested geography checked; facts remain unverified.',
                 status='partial', sources=sources,
                 data={'matches': matches[:8], 'match': match,
                       'provider_details': details[:5], 'place_id': place_id or None,
                       'verification_level': ('provider_details_candidate' if details else
                                              'candidate_match' if match else 'unverified'),
                       'ambiguous': ambiguous, 'geography_rejected': rejected},
                 limitations=['A matching ID or name does not verify admission, opening hours or accessibility.',
                              'Missing metadata is unknown, not proof a destination does not exist.'])
