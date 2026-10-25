"""Conservative recovery of model-generated daily schedules.

Only suggested clock times are shifted. Never shorten visits, invent venues,
move confirmed reservations, cross midnight, or claim transport verification.
This runs after a bounded model retry and never replaces strict validation.
"""
from __future__ import annotations

from copy import deepcopy
import re
from typing import Any

from core.pipeline_integrity import schedule_invariants

_CLOCK = re.compile(r'^(?:[01]?\d|2[0-3]):[0-5]\d$')
_FIXED = re.compile(r'\b(?:fixed|confirmed|reserved|non[- ]?refundable|pre[- ]?booked)\b', re.I)


def _minute(activity: dict) -> int | None:
    raw = activity.get('start_time') or activity.get('time')
    match = _CLOCK.fullmatch(str(raw or '').strip())
    if not match:
        return None
    h, m = [int(n) for n in match.group().split(':')]
    if h > 23:
        return None
    return 60 * h + m


def _set_minute(activity: dict, minute: int) -> None:
    clock = f'{minute // 60:02d}:{minute % 60:02d}'
    if activity.get('start_time') is not None:
        activity['start_time'] = clock
    label = str(activity.get('time') or '')
    if _CLOCK.fullmatch(label.strip()):
        activity['time'] = clock
    elif activity.get('start_time') is None:
        activity['time'] = clock


def _fixed_time(activity: dict) -> bool:
    if activity.get('fixed_time') is True or activity.get('confirmed_time') is True:
        return True
    # Departure events and transport legs may have externally constrained
    # service times. Never silently move an airport/train/ferry leg.
    if activity.get('activity_type') == 'transport':
        return True
    description = str(activity.get('description') or '')
    if re.search(r'\b(?:airport transfer|flight departure|board (?:a |the )?train|'
                 r'train departure|flight check-in|board (?:a |the )?ferry)\b',
                 description, re.I):
        return True
    return bool(_FIXED.search(' '.join(str(activity.get(field) or '')
                                      for field in ('booking_advice', 'verification_needed'))))


def _meal_time_safe(activity: dict, start: int) -> bool:
    text = ' '.join(str(activity.get(field) or '') for field in
                    ('meal_type', 'description')).casefold()
    if 'breakfast' in text:
        return 6 * 60 <= start <= 11 * 60 + 30
    if 'lunch' in text:
        return 11 * 60 <= start <= 15 * 60
    if 'dinner' in text:
        return 17 * 60 <= start <= 22 * 60
    return True


def recover_suggested_schedule(day: dict, *, max_individual_shift: int = 75,
                               max_total_shift: int = 120) -> tuple[dict | None, list[str]]:
    """Shift conflicting suggested starts forward, or fail closed.

    Respects per-activity inbound transfer AND safety buffer. Only acts on
    clocked, duration-bearing activities and keeps the original ordering.
    All changes are verified by schedule_invariants after adjustment.
    """
    original = day.get('activities')
    if not isinstance(original, list) or len(original) < 2:
        return None, []
    draft = deepcopy(day)
    activities = draft['activities']
    moved: list[str] = []
    total_shift = 0
    previous_end: int | None = None
    previous_start: int | None = None
    for index, item in enumerate(activities):
        if not isinstance(item, dict):
            return None, []
        start = _minute(item)
        duration = item.get('duration_minutes')
        if start is None or not isinstance(duration, int) or isinstance(duration, bool) or duration <= 0:
            # Never certify an unknown gap as repaired.
            return None, []
        if previous_start is not None and start < previous_start:
            return None, []
        if previous_end is not None:
            transfer = item.get('transfer_minutes', 0)
            buffer = item.get('buffer_minutes', 0)
            if any(isinstance(x, bool) or not isinstance(x, int) or x < 0
                   for x in (transfer, buffer)):
                return None, []
            required = previous_end + transfer + buffer
            if start < required:
                delta = required - start
                if (_fixed_time(item) or delta > max_individual_shift
                        or total_shift + delta > max_total_shift
                        or required + duration > 23 * 60
                        or not _meal_time_safe(item, required)):
                    return None, []
                _set_minute(item, required)
                total_shift += delta
                moved.append(f'Activity {index + 1}: shifted {delta} minutes later.')
                start = required
        previous_start = start
        previous_end = start + duration
    if not moved or schedule_invariants(draft):
        return None, []
    return draft, moved


def rewrite_internal_reasons(day: dict) -> tuple[dict, list[str]]:
    """Remove template/blueprint jargon without dropping a Why field.

    A new reason is strictly grounded in the existing description; it does not
    add unverified factual, booking, or geographic claims.
    """
    from core.pipeline_integrity import internal_reason_issue
    draft = deepcopy(day)
    changed: list[str] = []
    for index, activity in enumerate(draft.get('activities') or []):
        if not isinstance(activity, dict):
            continue
        reason = str(activity.get('why_this_fits') or '')
        if not reason or not internal_reason_issue({'day': day.get('day'), 'activities': [activity]}):
            continue
        detail = str(activity.get('description') or '').strip().rstrip('.')
        match = re.match(r'^(?:explore|visit|discover|see|enjoy|experience|walk through|tour|try)\s+(.+)',
                         detail, re.I)
        if match:
            complement = match.group(1)[:145].strip()
            revised = f'A chance to experience {complement[0].lower() + complement[1:]} firsthand.' if complement else ''
        else:
            name = str(activity.get('place_name') or activity.get('area') or '').strip()
            revised = f'Time to experience {name} as part of the day.' if name else ''
        if revised:
            activity['why_this_fits'] = revised[:240]
            changed.append(f'Activity {index + 1}: replaced internal planning language.')
    if internal_reason_issue(draft):
        return day, []
    return draft, changed


def recover_excursion_lodging(day: dict, *, previous_allocation: dict | None,
                              allocation: dict) -> tuple[dict | None, list[str]]:
    """Remove an invented local luggage drop-off on an unchanged-base day trip.

    Only an exact, bounded wording is eligible. The hotel base is taken from
    the validated blueprint; this does not confirm any hotel or train booking.
    Any other hotel claim, or ambiguous text, must be sent back for model repair.
    """
    if not previous_allocation:
        return None, []
    old_base = str(previous_allocation.get('overnight') or '').strip()
    base = str(allocation.get('overnight') or '').strip()
    visited = str(allocation.get('city') or '').strip()
    previous_city = str(previous_allocation.get('city') or '').strip()
    if (not old_base or not base or not visited or
            old_base.casefold() != base.casefold() or
            previous_city.casefold() == visited.casefold()):
        return None, []
    activities = day.get('activities') or []
    if not activities or not isinstance(activities[0], dict):
        return None, []
    first = activities[0]
    original = str(first.get('description') or '')
    # In the confirmed failure the destination-city hotel drop occurs after
    # arrival; remove only this precise unsupported claim, never a transfer.
    clause = re.compile(
        r'\s+and\s+drop\s+off\s+(?:your\s+|the\s+)?'
        r'(?:luggage|bags?|baggage)\s+at\s+(?:your\s+|the\s+|a\s+)?'
        r'(?:accommodation|hotel|lodging)'
        r'(?:\s+to\s+begin\s+the\s+day\s+unencumbered)?', re.I)
    if not clause.search(original):
        return None, []
    revised = deepcopy(day)
    replacement = clause.sub('', original, count=1)
    if replacement == original or not replacement.strip():
        return None, []
    revised['activities'][0]['description'] = replacement
    logistics = str(revised.get('logistics') or '').strip()
    # Do not certify contradictory itinerary notes as repaired.
    from core.pipeline_integrity import _DAYTRIP_LODGING_CLAIM
    if _DAYTRIP_LODGING_CLAIM.search(logistics):
        return None, []
    if not re.search(r'\breturn\s+(?:to\s+)?' + re.escape(base) + r'\b',
                     logistics, re.I):
        logistics = (logistics.rstrip('. ') + '. ' if logistics else '') + (
            f'Return to {base} for the overnight stay after this day trip; '
            'confirm the transport service before travel.')
    revised['logistics'] = logistics
    return revised, ['Removed unsupported excursion-city hotel/luggage claim.']
