"""Propose local, non-destructive itinerary repairs grounded in supplied alternatives."""
from __future__ import annotations

from aligo.skill_support import parse_time, reply, slug


async def run(ctx, args):
    itinerary = args.get('activities') or []
    if not isinstance(itinerary, list) or len(itinerary) > 50 or any(
            not isinstance(row, dict) for row in itinerary):
        return reply('Provide a list of up to 50 structured activities.', status='partial')
    disruption = args.get('disruption') or {}
    if not isinstance(disruption, dict) or not str(disruption.get('type') or '').strip():
        return reply('Provide a typed disruption.', status='partial')
    kind = slug(disruption['type'])
    affected = {slug(x) for x in (disruption.get('affected_places') or []) if slug(x)}
    try:
        delay = int(disruption.get('delay_minutes') or 0)
        start_index = int(disruption.get('start_activity_index', 0))
    except (ValueError, TypeError):
        return reply('Invalid delay or activity index.', status='partial')
    if not 0 <= delay <= 720 or not 0 <= start_index <= len(itinerary):
        return reply('Disruption index or delay outside supported bounds.', status='partial')
    choices = args.get('alternative_candidates') or []
    if not isinstance(choices, list):
        return reply('Alternative candidates must be a list.', status='partial')
    excluded = {slug(x) for x in (args.get('visited_names') or []) if slug(x)}
    alternatives = []
    for candidate in choices[:30]:
        if not isinstance(candidate, dict) or not slug(candidate.get('name')):
            continue
        if slug(candidate['name']) not in excluded and all(
                slug(candidate['name']) != slug(item.get('place_name') or item.get('place') or '')
                for item in itinerary):
            alternatives.append({'name': str(candidate['name'])[:150],
                                 'verification_level': 'supplied_candidate',
                                 'hours_status': 'not_checked',
                                 'booking_status': 'not_checked'})
    plan, flagged, fixed_conflicts = [], [], []
    for i, row in enumerate(itinerary):
        item = dict(row)
        name = slug(item.get('place_name') or item.get('place') or item.get('description'))
        targeted = (bool(affected and any(value == name or value in name for value in affected))
                    or kind in {'closure', 'cancellation'} and i == start_index and not affected)
        if targeted:
            flagged.append(i)
            item['replanning_action'] = 'replace_or_reschedule'
            if alternatives:
                item['replacement_candidates'] = alternatives[:3]
        elif delay and i >= start_index:
            value = parse_time(item.get('start_time'))
            if value is not None:
                if item.get('booking_confirmed') or item.get('fixed_time'):
                    fixed_conflicts.append(i)
                    item['replanning_action'] = 'fixed_reservation_requires_review'
                elif value+delay < 1440:
                    item['suggested_start_time'] = f'{(value+delay)//60:02d}:{(value+delay)%60:02d}'
                else:
                    flagged.append(i)
                    item['replanning_action'] = 'requires_next_day_review'
        plan.append(item)
    # The proposal is never automatically committed; preserve original descriptions.
    return reply('Local itinerary repair proposal, pending confirmation and schedule validation.',
                 status='partial',
                 data={'proposal': plan, 'affected_activity_indices': sorted(set(flagged)),
                       'fixed_time_conflict_indices': fixed_conflicts,
                       'replacement_candidates': alternatives[:5],
                       'shifted_activities': sum('suggested_start_time' in item for item in plan),
                       'requires_approval': True, 'severity': str(disruption.get('severity') or 'moderate'),
                       'disruption_type': kind},
                 limitations=['Closure/delay reports and candidate opening hours are not independently verified.',
                              'Check future visits, transfers and fixed reservations before accepting the revision.'])
