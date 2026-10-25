"""Rank mapped itinerary stops by relevance, novelty and known metadata.

The ranking is deterministic; mapped candidate evidence never means a venue is
open, suitable, bookable or independently fact-checked.
"""
from __future__ import annotations

from services.providers import ProviderUnavailable
from services.skill_support import coordinates, place_record, reply, slug


def _terms(value):
    if isinstance(value, (list, tuple)):
        return {slug(x) for x in value if slug(x)}
    return {slug(value)} if slug(value) else set()


def _identity(row):
    return (str(row.get('place_id') or row.get('osm_id') or '').strip()
            or f"{slug(row.get('name'))}|{slug(row.get('address'))}")


async def run(ctx, args):
    source = args.get('candidates')
    sources = []
    if source is None:
        if ctx.providers is None:
            return reply('Mapped place candidates or a places provider are required.', status='unavailable')
        try:
            location = coordinates(args)
            if location is None:
                destination = str(args.get('city') or args.get('destination') or
                                  getattr(ctx, 'fields', {}).get('destination') or '').strip()
                if not destination:
                    return reply('Specify a destination or coordinates.', status='partial')
                geocoded = await ctx.providers.request('geocode', place=destination)
                sources += geocoded.evidence
                matches = [r for r in geocoded.data.get('results', []) if isinstance(r, dict)]
                country = slug(args.get('country'))
                if country:
                    matches = [r for r in matches if slug(r.get('country')) == country]
                if len({(slug(r.get('country')), str(r.get('latitude')), str(r.get('longitude')))
                        for r in matches}) != 1:
                    return reply('Ambiguous or unknown city: supply a country or coordinates.',
                                 status='partial', sources=sources)
                location = float(matches[0]['latitude']), float(matches[0]['longitude'])
            radius = max(250, min(int(args.get('radius', 4500)), 15000))
            response = await ctx.providers.request('places', latitude=location[0],
                                                   longitude=location[1],
                                                   kind=str(args.get('kind') or 'tourism'), radius=radius)
            source = response.data.get('results', [])
            sources += response.evidence
        except (ValueError, TypeError, KeyError, ProviderUnavailable) as exc:
            return reply('Mapped place search unavailable.', status='unavailable',
                         limitations=[type(exc).__name__], sources=sources)
    if not isinstance(source, list):
        return reply('Candidate locations must be a list.', status='partial')
    try:
        limit = max(1, min(int(args.get('limit', 12)), 30))
    except (ValueError, TypeError):
        return reply('Invalid place limit.', status='partial')
    exclude = _terms(args.get('exclude_names')) | _terms(args.get('visited_names'))
    interests = _terms(args.get('interests')) | _terms(args.get('categories'))
    area = slug(args.get('area'))
    selected = {}
    rejected = {'excluded': 0, 'invalid': 0, 'duplicate': 0}
    for index, item in enumerate(source[:200]):
        if not isinstance(item, dict) or not slug(item.get('name')):
            rejected['invalid'] += 1
            continue
        name = slug(item['name'])
        if name in exclude or any(slug(a) in exclude for a in item.get('aliases', []) if isinstance(a, str)):
            rejected['excluded'] += 1
            continue
        identity = _identity(item)
        category = slug(' '.join(str(item.get(k) or '') for k in
                                 ('category', 'type', 'description', 'tags')))
        location = slug(' '.join(str(item.get(k) or '') for k in
                                 ('address', 'neighborhood', 'area')))
        score = 2 * sum(bool(term in category or term in name) for term in interests)
        score += 2 * int(bool(area and area in location))
        score += int(bool(item.get('place_id') or item.get('osm_id')))
        score += int(bool(item.get('address')))
        record = place_record(item)
        # Pass through supplier metadata as unverified evidence for optional
        # specialized Skills. No opening/booking/accessibility claim is certified.
        for field in ('weekly_hours', 'opening_hours', 'reservation_required',
                      'booking_url', 'ticket_price', 'currency', 'entry_windows',
                      'cuisine', 'specialties', 'season_months', 'accessibility'):
            value = item.get(field)
            if isinstance(value, (str, int, float, bool, list, dict)):
                record[field] = value[:160] if isinstance(value, str) else value

        record.update({'verification_level': 'mapped_candidate' if sources else 'supplied_candidate',
                       'matching_score': score, 'hours_status': 'not_checked',
                       'admission_status': 'not_checked', 'visit_status': 'not_confirmed'})
        candidate = (-score, index, record)
        if identity in selected:
            rejected['duplicate'] += 1
            if candidate[:2] < selected[identity][:2]:
                selected[identity] = candidate
        else:
            selected[identity] = candidate
    ranked = sorted(selected.values(), key=lambda t: t[:2])
    return reply('Itinerary attraction candidates ranked without inventing opening hours.',
                 status='partial', sources=sources,
                 data={'results': [x[2] for x in ranked[:limit]],
                       'verification_level': 'mapped_candidate' if sources else 'supplied_candidate',
                       'excluded_count': rejected['excluded'], 'candidate_diagnostics': rejected,
                       'ranked_count': len(ranked)},
                 limitations=['Suitability and visit readiness need independently checked venue details.',
                              'A name or mapping point is not proof of admission, opening or accessibility.'])
