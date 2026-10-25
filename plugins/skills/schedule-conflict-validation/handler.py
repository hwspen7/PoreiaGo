"""Exhaustively validate timed visits and incoming transfer-plus-safety buffers."""
from __future__ import annotations

from datetime import datetime
from services.skill_support import reply


def _minutes(value, name):
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 360:
        raise ValueError(f'Invalid {name}')
    return value


async def run(ctx, args):
    events = args.get('events') or []
    if not isinstance(events, list) or len(events) > 100:
        return reply('Provide no more than 100 structured events.', status='partial',
                     data={'conflicts': [], 'invalid_events': ['invalid_list']})
    normalized, invalid = [], []
    for i, row in enumerate(events):
        try:
            if not isinstance(row, dict):
                raise ValueError('Event must be an object')
            start = datetime.fromisoformat(str(row['start']))
            end = datetime.fromisoformat(str(row['end']))
            if end <= start:
                raise ValueError('End must be after start')
            # Legacy transfer_buffer_minutes is the outgoing buffer on the
            # previous event. New transfer_minutes / buffer_minutes apply to
            # the incoming leg of this event (do not double-count same field).
            outgoing = _minutes(row.get('transfer_buffer_minutes', 0), 'outgoing buffer')
            incoming = _minutes(row.get('transfer_minutes', 0), 'incoming transfer')
            safety = _minutes(row.get('buffer_minutes', 0), 'incoming safety buffer')
            normalized.append({'name': str(row.get('name') or f'Event {i+1}'),
                               'start': start, 'end': end, 'outgoing': outgoing,
                               'incoming': incoming+safety, 'index': i})
        except (KeyError, TypeError, ValueError, OverflowError):
            invalid.append(i)
    if (len({row['start'].tzinfo is None for row in normalized}) > 1 or
            any((row['start'].tzinfo is None) != (row['end'].tzinfo is None) for row in normalized)):
        return reply('Mixed timezone-aware and naive times cannot be compared.', status='partial',
                     data={'conflicts': [], 'checked_events': len(normalized),
                           'invalid_events': invalid + ['mixed_timezones']})
    normalized.sort(key=lambda row: (row['start'], row['end'], row['index']))
    conflicts = []
    for position, later in enumerate(normalized):
        for earlier in normalized[:position]:
            gap = (later['start'] - earlier['end']).total_seconds()/60
            if gap < 0:
                conflicts.append({'previous': earlier['name'], 'next': later['name'],
                                  'previous_index': earlier['index'], 'next_index': later['index'],
                                  'overlap_minutes': round(-gap, 2),
                                  'required_buffer_minutes': (max(earlier['outgoing'], later['incoming'])
                                                              if earlier is normalized[position-1] else 0),
                                  'reason': 'overlap'})
            if earlier is normalized[position-1]:
                needed = max(earlier['outgoing'], later['incoming'])
                if gap >= 0 and gap < needed:
                    conflicts.append({'previous': earlier['name'], 'next': later['name'],
                                      'previous_index': earlier['index'], 'next_index': later['index'],
                                      'overlap_minutes': 0, 'actual_gap_minutes': round(gap, 2),
                                      'required_buffer_minutes': needed,
                                      'shortfall_minutes': round(needed-gap, 2),
                                      'reason': 'insufficient_transfer_buffer'})
    return reply('All timed overlaps and specified transfer buffers examined.',
                 status='partial' if invalid else 'ok',
                 data={'conflicts': conflicts, 'checked_events': len(normalized),
                       'invalid_events': invalid},
                 limitations=['This does not verify the actual travel duration or opening time of a venue.'])
