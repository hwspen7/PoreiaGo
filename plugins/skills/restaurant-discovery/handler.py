"""Filter and rank restaurant candidates without inventing dietary or booking assurances."""
from __future__ import annotations

from services.providers import ProviderUnavailable
from services.skill_support import coordinates, place_record, reply, slug


def _words(value):
    if isinstance(value, (tuple, list)):
        return {slug(v) for v in value if slug(v)}
    return {slug(value)} if slug(value) else set()


async def run(ctx, args):
    city = str(args.get('city') or args.get('destination') or
               getattr(ctx, 'fields', {}).get('destination') or '').strip()
    source = args.get('candidates')
    sources = []
    if source is None:
        if ctx.providers is None:
            return reply('Supply restaurant candidates or connect a places provider.', status='unavailable')
        try:
            location = coordinates(args)
            if location is None:
                if not city:
                    return reply('Specify a city or coordinates.', status='partial')
                geo = await ctx.providers.request('geocode', place=city)
                sources += geo.evidence
                matches = [x for x in geo.data.get('results', []) if isinstance(x, dict)]
                if args.get('country'):
                    matches = [x for x in matches if slug(x.get('country')) == slug(args['country'])]
                if len({(slug(x.get('country')), str(x.get('latitude')), str(x.get('longitude')))
                        for x in matches}) != 1:
                    return reply('Ambiguous or missing city.', status='partial', sources=sources)
                location = float(matches[0]['latitude']), float(matches[0]['longitude'])
            radius = max(200, min(int(args.get('radius', 4500)), 10000))
            found = await ctx.providers.request('places', latitude=location[0],
                                                longitude=location[1], kind='restaurant', radius=radius)
            sources += found.evidence
            source = found.data.get('results', [])
        except (ValueError, TypeError, KeyError, ProviderUnavailable) as exc:
            return reply('Restaurant discovery unavailable.', status='unavailable',
                         sources=sources, limitations=[type(exc).__name__])
    if not isinstance(source, list):
        return reply('Restaurant candidates must be a list.', status='partial')
    try:
        limit = max(1, min(int(args.get('limit', 10)), 20))
    except (ValueError, TypeError):
        return reply('Invalid result limit.', status='partial')
    cuisine = slug(args.get('cuisine'))
    area = slug(args.get('area'))
    dietary = slug(args.get('dietary_requirement'))
    excluded = _words(args.get('exclude_names')) | _words(args.get('already_planned'))
    scored, seen, omitted = [], set(), {'duplicate': 0, 'excluded': 0, 'unmatched_cuisine': 0}
    for index, row in enumerate(source[:160]):
        if not isinstance(row, dict) or not slug(row.get('name')):
            continue
        name = slug(row['name'])
        identity = str(row.get('place_id') or row.get('osm_id') or f"{name}|{slug(row.get('address'))}")
        if name in excluded:
            omitted['excluded'] += 1
            continue
        if identity in seen:
            omitted['duplicate'] += 1
            continue
        seen.add(identity)
        tags = slug(' '.join(str(row.get(key) or '') for key in ('cuisine', 'category', 'description')))
        # Unknown cuisine is not treated as a confirmed mismatch; retain but rank lower.
        if cuisine and tags and cuisine not in tags:
            omitted['unmatched_cuisine'] += 1
            continue
        location = slug(' '.join(str(row.get(key) or '') for key in ('area', 'address', 'neighborhood')))
        score = 3 * int(bool(cuisine and cuisine in tags))
        score += 2 * int(bool(area and area in location))
        score += int(bool(row.get('address'))) + int(bool(row.get('place_id') or row.get('osm_id')))
        rec = place_record(row)
        for field in ('weekly_hours', 'opening_hours', 'reservation_required',
                      'booking_url', 'ticket_price', 'currency', 'entry_windows',
                      'season_months', 'accessibility', 'specialties'):
            value = row.get(field)
            if isinstance(value, (str, int, float, bool, list, dict)):
                rec[field] = value[:160] if isinstance(value, str) else value

        if row.get('cuisine'):
            rec['cuisine'] = row['cuisine']
        rec.update({'matching_score': score, 'verification_level': 'mapped_candidate' if sources else
                    'supplied_candidate', 'dietary_status': 'not_checked' if dietary else 'not_requested',
                    'opening_hours_status': 'not_checked', 'booking_status': 'not_checked',
                    'price_status': 'not_checked'})
        scored.append((-score, index, rec))
    scored.sort(key=lambda entry: entry[:2])
    return reply('Unique restaurant candidates ranked for the day; availability unverified.',
                 status='partial', sources=sources,
                 data={'results': [x[2] for x in scored[:limit]], 'city': city,
                       'verification_level': 'mapped_candidate' if sources else 'supplied_candidate',
                       'constraints_applied': {'cuisine': cuisine or None, 'area': area or None,
                                               'excluded_names': len(excluded)},
                       'candidate_diagnostics': omitted, 'dietary_requirement': dietary or None},
                 limitations=['Allergen safety, menus, opening hours, prices and reservations are unverified.',
                              'A missing cuisine tag is unknown, not proof of dietary suitability.'])
