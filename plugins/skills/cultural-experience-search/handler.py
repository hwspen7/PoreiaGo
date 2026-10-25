"""Prioritize evidence-backed cultural experiences without inventing workshop sessions."""
from __future__ import annotations

from services.providers import ProviderUnavailable
from services.skill_support import coordinates, place_record, reply, slug


def _terms(value):
    if isinstance(value, (list, tuple)):
        return {slug(x) for x in value if slug(x)}
    return {slug(value)} if slug(value) else set()


async def run(ctx, args):
    source = args.get('candidates')
    sources = []
    if source is None:
        if ctx.providers is None:
            return reply('Cultural venue source unavailable.', status='unavailable')
        try:
            point = coordinates(args)
            if point is None:
                return reply('Supply venue candidates or location coordinates.', status='partial')
            found = await ctx.providers.request('places', latitude=point[0], longitude=point[1],
                                                kind='tourism', radius=4500)
            source = found.data.get('results', [])
            sources = found.evidence
        except (ProviderUnavailable, ValueError, TypeError, KeyError) as exc:
            return reply('Cultural candidate lookup unavailable.', status='unavailable',
                         limitations=[type(exc).__name__])
    if not isinstance(source, list):
        return reply('Cultural candidates must be a list.', status='partial')
    interests = _terms(args.get('interest')) | _terms(args.get('interests'))
    excluded = _terms(args.get('exclude_names')) | _terms(args.get('visited_names'))
    try:
        limit = max(1, min(int(args.get('limit', 8)), 16))
    except (TypeError, ValueError):
        return reply('Invalid cultural venue limit.', status='partial')
    ranked, seen = [], set()
    for index, row in enumerate(source[:140]):
        if not isinstance(row, dict) or not slug(row.get('name')):
            continue
        name = slug(row['name'])
        identity = str(row.get('place_id') or row.get('osm_id') or f"{name}|{slug(row.get('address'))}")
        if name in excluded or identity in seen:
            continue
        seen.add(identity)
        tags = slug(' '.join(str(row.get(k) or '') for k in
                             ('category', 'type', 'description', 'experience_tags')))
        if interests and not any(term in tags or term in name for term in interests):
            continue
        booking_url = str(row.get('booking_url') or '').strip()
        organizer = str(row.get('organizer') or '').strip()
        explicit_session = bool(row.get('session_id') or row.get('event_id'))
        score = 3 * sum(bool(term in tags or term in name) for term in interests)
        score += int(bool(row.get('place_id') or row.get('osm_id')))
        score += 2 * int(bool(organizer and booking_url)) + int(explicit_session)
        record = place_record(row)
        record.update({'matching_score': score,
                       'verification_level': 'mapped_candidate' if sources else 'supplied_candidate',
                       'booking_reference_present': bool(organizer and booking_url),
                       'booking_status': 'not_checked', 'event_date_status': 'not_checked',
                       'is_bookable_workshop': False, 'specific_session_identified': explicit_session})
        if organizer:
            record['organizer_candidate'] = organizer[:150]
        if row.get('category'):
            record['experience_category'] = row['category']
        ranked.append((-score, index, record))
    ranked.sort(key=lambda row: row[:2])
    return reply('Cultural venues ranked by interest with explicit workshop verification gaps.',
                 status='partial', sources=sources,
                 data={'results': [x[2] for x in ranked[:limit]],
                       'interest': args.get('interest'), 'excluded_venues': len(excluded),
                       'specific_session_candidates': sum(x[2]['specific_session_identified'] for x in ranked)},
                 limitations=['A cultural venue is not proof of a visitor workshop or an available session.',
                              'Organizer, date, language and ticket conditions require direct confirmation.'])
