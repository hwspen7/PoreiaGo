"""Bounded, evidence-safe repair after a newly created trip fails the commit gate.

The repair changes only rejected days. It never weakens or bypasses the
final validator and never upgrades model-supplied evidence to verified.
"""
from __future__ import annotations

import asyncio
import json
import logging
from copy import deepcopy
from typing import Any

from intelligence.models import ModelUnavailable
from planning.segmented_planning import DAY_SYSTEM
from planning.trip_state import TripValidationError, validate_segment_days
from planning.validation_gate import ItineraryGateError, validate_for_commit

logger = logging.getLogger(__name__)


def _untrusted_evidence_to_unverified(document: dict, issues: list) -> dict:
    """A generated link is not a verified observation; revoke that assertion."""
    revised = deepcopy(document)
    for issue in issues:
        if issue.code != 'UNTRUSTED_VERIFIED_EVIDENCE':
            continue
        if not isinstance(issue.day, int) or not isinstance(issue.activity_index, int):
            continue
        try:
            activity = revised['days'][issue.day - 1]['activities'][issue.activity_index - 1]
        except (IndexError, KeyError, TypeError):
            continue
        if activity.get('evidence_status') == 'verified':
            activity['evidence_status'] = 'unverified'
    return revised


def _day_response(payload: Any, day_number: int) -> dict:
    if isinstance(payload, dict) and 'itinerary' in payload:
        payload = payload['itinerary']
    if not isinstance(payload, dict):
        raise TripValidationError('Create repair must return one structured day.')
    rows = payload.get('days')
    if not isinstance(rows, list) or len(rows) != 1:
        raise TripValidationError('Create repair must return exactly one day.')
    return validate_segment_days({'days': rows}, first=day_number, last=day_number)[0]


async def repair_created_itinerary(models: Any, document: dict, rejection: ItineraryGateError,
                                   *, request: str, fields: dict, preferences: dict,
                                   timeout: float = 65.0, max_days: int = 2) -> dict:
    """Attempt one schema-guided repair for each of at most two rejected days.

    Other days are byte-for-byte equal in their normalized representation.
    Failed attempts leave the original candidate untouched. For global issues
    without a specific day, fail closed instead of guessing which day to edit.
    """
    candidate = _untrusted_evidence_to_unverified(document, rejection.issues)
    try:
        return validate_for_commit(candidate)
    except ItineraryGateError as rejection_after_evidence:
        remaining = rejection_after_evidence

    day_numbers = sorted({issue.day for issue in remaining.issues
                          if type(issue.day) is int})
    if (not day_numbers or len(day_numbers) > max_days or
            any(type(issue.day) is not int for issue in remaining.issues)):
        raise remaining

    for number in day_numbers:
        applicable = [issue.as_dict() for issue in remaining.issues
                      if issue.day == number]
        day = candidate['days'][number - 1]
        prompt = json.dumps({
            'task': 'repair_one_created_day_after_precommit_rejection',
            'user_request': request,
            'destination': candidate['destination'],
            'day_number': number,
            'total_days': len(candidate['days']),
            'existing_rejected_day': day,
            'previous_day': candidate['days'][number - 2] if number > 1 else None,
            'following_day': candidate['days'][number] if number < len(candidate['days']) else None,
            'explicit_constraints': fields,
            'user_preferences': preferences,
            'validation_issues': applicable,
            'instructions': (
                'Return exactly one complete replacement day in {"days":[...]}. '
                'Correct every listed validation issue while retaining unrelated '
                'activities, traveler preferences, destination, and day identity. '
                'Keep travel gaps consistent with known durations and do not '
                'invent transfer minutes, opening hours, tickets, or reservations. '
                'If a verified source was untrusted, do not label it verified.'),
        }, ensure_ascii=False, default=str)
        try:
            if callable(getattr(models, 'complete_daily_detail', None)):
                response = await asyncio.wait_for(
                    models.complete_daily_detail(DAY_SYSTEM, prompt,
                                                 day_number=number, compact=True),
                    timeout=timeout)
            else:
                response = await asyncio.wait_for(
                    models.complete('reasoning', DAY_SYSTEM, prompt, json_mode=True),
                    timeout=timeout)
            replacement = _day_response(response, number)
        except (ModelUnavailable, asyncio.TimeoutError, TypeError, ValueError, KeyError) as exc:
            logger.warning('Create precommit day %d repair unavailable: %s',
                           number, type(exc).__name__)
            raise remaining from exc
        candidate['days'][number - 1] = replacement
        try:
            validate_for_commit(candidate)
        except ItineraryGateError as next_rejection:
            remaining = next_rejection
            if any(issue.day == number for issue in next_rejection.issues):
                raise next_rejection
        else:
            return candidate
    return validate_for_commit(candidate)
