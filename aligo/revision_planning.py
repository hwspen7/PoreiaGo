"""Bounded, model-directed day-level edits to an existing validated itinerary.

The model chooses which days to edit and writes only those days.  The server
validates each replacement against a private copy of the original trip; no
partial result is persisted.  Nothing in this module assumes a destination,
language, particular provider, or a particular itinerary length.
"""
from __future__ import annotations

import asyncio
import json
import os
from copy import deepcopy

from .models import ModelUnavailable
from .trip_state import TripValidationError, validate_itinerary
from .activity_revision import ACTIVITY_PATCH_SYSTEM, activity_patch_schema, merge_activity_patch
from .validation_gate import validate_for_commit, explicit_revision_intent, ItineraryGateError
from .time_contract import normalized_clock_activity, activity_start


REVISION_SCOPE_SYSTEM = """You are a multilingual itinerary edit scope planner.
Return only a JSON object with a target_days array of day numbers.
Choose the smallest set of days that can satisfy the LATEST user edit. An edit
without an explicit day still needs affected days. For a whole-trip request,
select all affected days. Never change the destination, trip length or unrelated
days. Base selection on the supplied day index, not imagined attractions.
Do not write activities, prose, markdown, or a full itinerary.
"""

REVISION_DAY_SYSTEM = """You are Aligo's specialist itinerary editor.
Return ONLY one complete replacement day as a JSON object, with keys day,
title, activities, and optional notes, pace, area_cluster, logistics,
weather_alternative and other supported day fields. Each activity must have
at least a nonempty description. Optional activity fields include start_time
(HH:MM), duration_minutes (integer), why_this_fits, transport_hint,
verification_needed and place_name. Keep all user constraints and preserve
valuable existing stops unless they conflict with the requested edit. For an
added activity, adjust the schedule and transfers realistically; make no
unsupported claims about opening hours, ticket prices or live availability.
Include specific named places when well supported. Respect the user's pace;
do not invent mandatory meal breaks. Write in the requested output language.
Never output the whole trip, other days, an itinerary wrapper, explanations,
markdown, or internal reasoning. Never change the day number.
"""


def _positive_int_env(name: str, default: int, cap: int) -> int:
    try:
        return max(1, min(cap, int(os.getenv(name, str(default)))))
    except (ValueError, TypeError):
        return default


def day_schema(day_number: int) -> dict:
    """Compact response schema, avoiding an expensive full-trip output."""
    activity = {
        'type': 'object',
        'properties': {
            'description': {'type': 'string'},
            'start_time': {'type': 'string'},
            'duration_minutes': {'type': 'integer'},
            'why_this_fits': {'type': 'string'},
            'transport_hint': {'type': 'string'},
            'verification_needed': {'type': 'string'},
            'place_name': {'type': 'string'},
            'venue_category': {'type': 'string'},
            'area': {'type': 'string'},
            'activity_type': {'type': 'string'},
            'meal_type': {'type': 'string'},
            'transfer_minutes': {'type': 'integer'},
            'buffer_minutes': {'type': 'integer'},
            'booking_advice': {'type': 'string'},
            'cost_level': {'type': 'string'},
        },
        'required': ['description'],
        'additionalProperties': False,
    }
    return {
        'type': 'object',
        'properties': {
            'day': {'type': 'integer', 'enum': [day_number]},
            'title': {'type': 'string'},
            'activities': {'type': 'array', 'items': activity, 'minItems': 1, 'maxItems': 12},
            'notes': {'type': 'string'},
            'area_cluster': {'type': 'string'},
            'logistics': {'type': 'string'},
            'weather_alternative': {'type': 'string'},
            'pace': {'type': 'string', 'enum': ['light', 'balanced', 'full']},
        },
        'required': ['day', 'title', 'activities'],
        'additionalProperties': False,
    }


def scope_schema(total_days: int) -> dict:
    return {'type': 'object', 'properties': {
        'target_days': {'type': 'array', 'items': {
            'type': 'integer', 'minimum': 1, 'maximum': total_days},
            'minItems': 1, 'maxItems': total_days}},
        'required': ['target_days'], 'additionalProperties': False}


def _clock_label(minutes: int) -> str:
    return f'{minutes // 60:02d}:{minutes % 60:02d}'


def day_schedule_context(activities: list[dict], *,
                         not_before_minute: int | None = None) -> dict:
    """Describe occupied intervals and raw gaps without inventing transfers.

    The intervals are observational, not recommendations. A free clock gap
    does not establish that distant attractions can be connected in that time.
    """
    from .pipeline_integrity import _timed_activity

    occupied = []
    missing = []
    for position, activity in enumerate(activities, 1):
        timed = _timed_activity(activity)
        if timed:
            occupied.append({'position': position, 'start_time': _clock_label(timed[0]),
                             'end_time': _clock_label(timed[1]),
                             'area': activity.get('area', ''),
                             'description': activity.get('description', '')[:130]})
        else:
            missing.append(position)
    occupied.sort(key=lambda record: record['start_time'])
    gaps = []
    prior_end = 0
    for item in occupied:
        start = int(item['start_time'][:2]) * 60 + int(item['start_time'][3:])
        if start > prior_end:
            gaps.append({'from': _clock_label(prior_end), 'to': _clock_label(start),
                         'minutes': start - prior_end})
        end = int(item['end_time'][:2]) * 60 + int(item['end_time'][3:])
        prior_end = max(prior_end, end)
    if prior_end < 1440:
        gaps.append({'from': _clock_label(prior_end), 'to': '24:00',
                     'minutes': 1440 - prior_end})
    result = {'occupied_intervals': occupied, 'raw_unoccupied_gaps': gaps,
              'unknown_interval_positions': missing,
              'warning': ('These are clock gaps only. Account for transfers, '
                          'opening hours and necessary buffers before inserting a stop. '
                          'You may replace or move existing activities instead.')}
    if isinstance(not_before_minute, int) and 0 <= not_before_minute < 1440:
        result['user_not_before_exclusive'] = _clock_label(not_before_minute)
        eligible = []
        for gap in gaps:
            start_hour, start_minute = map(int, gap['from'].split(':'))
            finish_hour, finish_minute = map(int, gap['to'].split(':'))
            eligible_start = max(start_hour * 60 + start_minute,
                                 not_before_minute + 1)
            finish = finish_hour * 60 + finish_minute
            if eligible_start < finish:
                eligible.append({'from': _clock_label(eligible_start),
                                 'to': gap['to'],
                                 'minutes': finish - eligible_start})
        result['raw_gaps_after_requested_time'] = eligible
        earliest = max(not_before_minute + 1, prior_end)
        if earliest < 1440:
            result['earliest_after_last_scheduled_stop'] = _clock_label(earliest)
        else:
            result['no_remaining_clock_time_after_last_stop'] = True
    return result


def _day_index(itinerary: dict) -> list[dict]:
    return [{'day': d['day'], 'title': d['title'],
             'area': d.get('area_cluster', ''),
             'stops': [a['description'][:95] for a in d['activities'][:8]]}
            for d in itinerary['days']]


def _selected_day_numbers(parsed: dict, total_days: int) -> list[int]:
    if not isinstance(parsed, dict) or not isinstance(parsed.get('target_days'), list):
        raise TripValidationError('The edit scope model must choose target days.')
    numbers = parsed['target_days']
    if not numbers or any(type(n) is not int or n < 1 or n > total_days for n in numbers):
        raise TripValidationError('The edit scope contains invalid target days.')
    # The model controls scope, but duplicate suggestions must not repeat work.
    return sorted(set(numbers))


def _replacement_day(candidate: object, target: int) -> dict:
    """Accept direct day JSON, or a one-day delta from compatible providers."""
    if isinstance(candidate, dict) and isinstance(candidate.get('day'), dict):
        candidate = candidate['day']
    if isinstance(candidate, dict) and isinstance(candidate.get('itinerary'), dict):
        candidate = candidate['itinerary']
    if isinstance(candidate, dict) and isinstance(candidate.get('changes'), list):
        entries = candidate['changes']
        if len(entries) != 1:
            raise TripValidationError('A day-edit response must contain exactly one day.')
        candidate = entries[0]
    if not isinstance(candidate, dict) or candidate.get('day') != target:
        raise TripValidationError(f'The editor must return day {target} only.')
    if 'days' in candidate or 'changes' in candidate:
        raise TripValidationError('A day edit may not output the entire itinerary.')
    return candidate


async def generate_revision(models, baseline: dict, query: str, *,
                            explicit_day: int | None, fields: dict,
                            preferences: dict, evidence: list,
                            timeout: float, protected_constraints=None) -> dict:
    """Generate candidate revision atomically without touching the persistent store.

    One model call per edited day, with a fresh token allowance. On an invalid
    or truncated day, retry only that day once with a compact request. If any
    day fails, the caller receives an exception and saves nothing.
    """
    total = len(baseline['days'])
    if explicit_day is not None:
        targets = [explicit_day]
    elif total == 1:
        targets = [1]
    else:
        scope_request = {'request': query, 'destination': baseline['destination'],
                         'days': _day_index(baseline),
                         'saved_preferences': preferences}
        try:
            scope = await asyncio.wait_for(
                models.complete_revision_scope(
                    REVISION_SCOPE_SYSTEM,
                    json.dumps(scope_request, ensure_ascii=False, default=str),
                    response_schema=scope_schema(total)), timeout=timeout)
        except (ModelUnavailable, asyncio.TimeoutError) as exc:
            reason = str(exc)
            code = ('OUTPUT_TRUNCATED' if 'OUTPUT_TRUNCATED' in reason else
                    'MODEL_TIMEOUT' if isinstance(exc, asyncio.TimeoutError) or 'TIMEOUT' in reason else
                    'MODEL_UNAVAILABLE')
            raise TripValidationError(f'MODEL_GENERATION_FAILED:{code}') from exc
        targets = _selected_day_numbers(scope, total)
    result = deepcopy(baseline)
    max_updates = _positive_int_env('ALIGO_REVISION_MAX_DAYS', 30, 30)
    if len(targets) > max_updates:
        raise TripValidationError('REVISION_SCOPE_EXCEEDS_CONFIGURED_LIMIT')
    for day_number in targets:
        base_day = baseline['days'][day_number - 1]
        if callable(getattr(models, 'complete_revision_patch', None)):
            try:
                result = await _generate_activity_patch(
                    models, result, baseline, query, day_number=day_number,
                    fields=fields, preferences=preferences, evidence=evidence,
                    timeout=timeout, enforce_intent=len(targets) == 1,
                    defer_global=len(targets) > 1,
                    protected_constraints=protected_constraints)
            except TripValidationError as exc:
                if getattr(exc, 'draft_candidate', None) is None and result != baseline:
                    exc.draft_candidate = result
                raise
            continue
        request = {
            'request': query, 'destination': baseline['destination'],
            'day_to_edit': base_day,
            'other_days_index': [row for row in _day_index(baseline)
                                 if row['day'] != day_number],
            'saved_preferences': preferences,
            'explicit_constraints': fields,
            'skill_evidence': evidence[:4],
            'output_language': fields.get('output_language', 'auto'),
        }
        errors = []
        for compact in (False, True):
            payload = dict(request)
            if compact:
                payload['skill_evidence'] = []
                payload['other_days_index'] = []
                payload['prior_error'] = errors[-1][:220]
                payload['instruction'] = ('Correct only this day. Return compact valid JSON. '
                                          'Preserve feasible original stops and user constraints.')
            try:
                raw = await asyncio.wait_for(models.complete_revision_day(
                    REVISION_DAY_SYSTEM, json.dumps(payload, ensure_ascii=False, default=str),
                    day_number=day_number, response_schema=day_schema(day_number),
                    compact=compact), timeout=timeout)
                replacement = _replacement_day(raw, day_number)
                proposed = validate_itinerary({'changes': [replacement]}, previous=result,
                                              operation='revise')
                if proposed['days'][day_number - 1] == result['days'][day_number - 1]:
                    raise TripValidationError('The requested day was not modified.')
                result = proposed
                break
            except (ModelUnavailable, TripValidationError, asyncio.TimeoutError) as exc:
                message = str(exc)
                errors.append(message)
                if isinstance(exc, (ModelUnavailable, asyncio.TimeoutError)):
                    code = ('OUTPUT_TRUNCATED' if 'OUTPUT_TRUNCATED' in message else
                            'INVALID_MODEL_JSON' if 'INVALID_MODEL_JSON' in message else
                            'EMPTY_MODEL_RESPONSE' if 'EMPTY_MODEL_RESPONSE' in message else
                            'MODEL_TIMEOUT' if isinstance(exc, asyncio.TimeoutError) or
                            'TIMEOUT' in message else 'MODEL_UNAVAILABLE')
                    # Retrying a missing model, failed connection or expired request
                    # doubles latency without changing the request contract.
                    if compact or code not in {'OUTPUT_TRUNCATED', 'INVALID_MODEL_JSON',
                                                'EMPTY_MODEL_RESPONSE'}:
                        raise TripValidationError(f'MODEL_GENERATION_FAILED:{code}') from exc
                elif compact:
                    raise TripValidationError(
                        f'REVISION_DAY_FAILED:day={day_number}: '
                        f'{type(exc).__name__}: {message[:180]}') from exc
    # A multi-day revision checks explicit intent against the complete proposal,
    # not against intermediate days, some of which need not contain the topic.
    if len(targets) > 1:
        from .revision_consistency import clear_inherited_stale_references
        result = clear_inherited_stale_references(result, baseline)
        try:
            validate_for_commit(result, previous=baseline,
                                intent=explicit_revision_intent(query),
                                protected_constraints=protected_constraints)
        except TripValidationError as exc:
            exc.draft_candidate = result
            raise
    return result


async def _recover_entire_day(models, result, baseline, query, *, day_number,
                              fields, preferences, timeout, prior_error,
                              intent, enforce_intent, protected_constraints=None):
    """One bounded full-day replan after a delta cannot resolve a real conflict.

    A daypart-only baseline provides no valid gaps to insert into. Replacing
    just the affected day can supply a complete schedule without silently
    changing the destination or other days. This is a model proposal, not a
    server-authored guess at opening times or journeys.
    """
    from .revision_consistency import clear_inherited_stale_references
    prompt = {
        'request': query,
        'day_to_edit': result['days'][day_number - 1],
        'destination': baseline['destination'],
        'other_days_index': [row for row in _day_index(baseline)
                             if row['day'] != day_number],
        'saved_preferences': preferences,
        'protected_requirements': fields.get('protected_journey_constraints', []),
        'observable_revision_constraints': intent,
        'prior_failure': prior_error[:850],
        'instruction': (
            'Return a complete replacement for this ONE day. Give every stop '
            'a practical HH:MM start and integer duration, preserve important '
            'existing visits and explicit traveler time anchors, and fulfill '
            'the new request with real activities. Reduce or replace '
            'lower-priority stops only when needed. Include enough time '
            'between physically distinct venues. Treat times as proposals, '
            'not verified opening hours; never invent external evidence.'),
        'output_language': fields.get('output_language', 'auto'),
    }
    raw = await asyncio.wait_for(models.complete_revision_day(
        REVISION_DAY_SYSTEM, json.dumps(prompt, ensure_ascii=False, default=str),
        day_number=day_number, response_schema=day_schema(day_number),
        compact=False), timeout=timeout)
    replacement = _replacement_day(raw, day_number)
    proposal = validate_itinerary({'changes': [replacement]}, previous=result,
                                  operation='revise')
    proposal = clear_inherited_stale_references(proposal, baseline)
    if enforce_intent:
        validate_for_commit(proposal, previous=baseline, intent=intent,
                            protected_constraints=protected_constraints)
    else:
        validate_for_commit(proposal, protected_constraints=protected_constraints)
    return proposal


async def _generate_activity_patch(models, result, baseline, query, *, day_number,
                                   fields, preferences, evidence, timeout, enforce_intent=False,
                                   defer_global=False, protected_constraints=None):
    """Use a compact activity delta; retry only bounded recoverable outputs."""
    original = baseline['days'][day_number - 1]
    indexed = [{'position': index, 'description': row['description'],
                'start_time': activity_start(row),
                'duration_minutes': row.get('duration_minutes'),
                'transfer_minutes': row.get('transfer_minutes'),
                'buffer_minutes': row.get('buffer_minutes'),
                'transport_hint': row.get('transport_hint'),
                'area': row.get('area'), 'place_name': row.get('place_name'),
                'venue_category': row.get('venue_category'),
                'activity_type': row.get('activity_type'),
                'meal_type': row.get('meal_type')}
               for index, row in enumerate(original['activities'], 1)]
    observable_intent = explicit_revision_intent(query)
    context = {
        'request': query, 'destination': baseline['destination'],
        'day_number': day_number,
        'day_title': original['title'],
        'day_metadata': {k: original.get(k) for k in
                         ('logistics', 'notes', 'intercity_transfer', 'overnight_base',
                          'area_cluster', 'weather_alternative')},
        'trip_metadata': {k: baseline.get(k) for k in
                          ('summary', 'budget_strategy', 'arrival_departure_plan')},
        'existing_activities': indexed,
        'other_days_index': [row for row in _day_index(baseline)
                             if row['day'] != day_number],
        'explicit_constraints': fields,
        'saved_preferences': preferences,
        'skill_evidence': evidence[:3],
        'output_language': fields.get('output_language', 'auto'),
        'observable_revision_constraints': observable_intent,
        'protected_journey_constraints': protected_constraints or [],
        'activity_edit_contract': {
            'indices': 'All positions refer to the ORIGINAL activity list (one-based).',
            'new_activity_required': ['description', 'start_time', 'duration_minutes'],
            'start_time_format': '24-hour HH:MM, e.g. 21:30 (not after 8pm)',
            'duration_minutes_format': 'integer between 10 and 720',
            'replace_policy': 'Select the existing source activity that matches the user request, not an arbitrary stop.',
            'topic_additions': 'Add a genuinely distinct qualifying venue, not merely a topic word in an old description.',
            'venue_category': 'Supply the specific venue_category when known and ensure the description and place match it.',
        },
        'schedule_context': day_schedule_context(
            original['activities'],
            not_before_minute=observable_intent.get('not_before_minute')),
    }
    from .working_drafts import review_draft
    _, baseline_issues = review_draft(result)
    context['baseline_review_issues'] = baseline_issues
    if observable_intent.get('early_topic'):
        context['activity_edit_contract']['early_visit'] = (
            'Place an ACTUAL activity of the requested category near the specified morning clock. '
            'Do not claim a new park or museum exists in notes when it is absent from activities.')
    if observable_intent.get('anchor_place'):
        context['activity_edit_contract']['anchored_visit'] = (
            'Make sure the requested named visit exists in activities and starts at the explicit '
            'requested clock. Do not substitute a mention in a note for a real activity.')
    if observable_intent.get('less_rushed'):
        context['activity_edit_contract']['pace_relief'] = (
            'Reduce a lower-priority stop or materially reduce total dwell time. '
            'Retiming all the same stops by one hour is not sufficient. Preserve traveler-anchored starts.')
    context['instruction'] = (
        'Implement the requested change and repair existing conflicts if possible. '
        'Preserve constraints from the current Journey. Identify venue category for '
        'new stops and use original positions for edit operations. Scheduling may '
        'be reconciled by clock time but transport and opening hours are unverified.')
    previous_issue = ''
    prior_operations = []
    previous_rejection = {}
    attempt_history = []
    latest_editable_candidate = None
    attempt_limit = _positive_int_env('ALIGO_REVISION_REPAIR_ATTEMPTS', 4, 6)
    for attempt in range(attempt_limit):
        compact = attempt > 0
        patch = None
        request = dict(context)
        if compact:
            request['skill_evidence'] = evidence[:2]
            request['prior_error'] = previous_issue[:1000]
            request['previous_operations'] = prior_operations[:12]
            request['previous_rejection'] = previous_rejection
            request['previous_patch'] = attempt_history[-1].get('candidate_patch') if attempt_history else None
            if previous_rejection.get('error_code') == 'REVISION_NEW_ACTIVITY_MISSING_START_TIME':
                request['required_repair'] = (
                    'The proposed new stop has no exact clock. Supply start_time in '
                    '24-hour HH:MM (for example 21:30), duration_minutes as an integer, '
                    'and a conflict-free schedule. Do not write only after 8pm.')
            elif previous_rejection.get('error_code') in {'REVISION_TOPIC_REQUIREMENT_NOT_MET', 'REVISION_ADDITION_NOT_MET'}:
                request['required_repair'] = (
                    'The prior operations did not add a distinct venue of the requested '
                    'topic. Use an actual qualifying new activity or change the selected '
                    'visits so the number of distinct topic venues increases.')
            if previous_rejection.get('error_code') in {'ACTIVITY_TIME_ORDER', 'SCHEDULE_INVARIANT',
                                                        'REVISION_ACTIVITY_TIME_CONFLICT'}:
                request['required_repair'] = (
                    'Read the previous attempted schedule and fix the exact overlap. '
                    'You may move or replace lower-priority stops, or select a gap. '
                    'All proposed activities need a concrete clock and duration.')
            if previous_rejection.get('error_code') == 'REVISION_ANCHORED_VISIT_NOT_MET':
                request['required_repair'] = (
                    'The user requested a particular place at an exact time. Include that venue '
                    'as an actual activity at that time. Do not only mention it in notes.')
            if previous_rejection.get('error_code') == 'REVISION_EARLY_TOPIC_NOT_MET':
                request['required_repair'] = (
                    'Add or move an ACTUAL activity with the requested category into the morning time window. '
                    'The activity must appear in the activities array, not only in notes.')
            if previous_rejection.get('error_code') == 'REVISION_PACE_NOT_IMPROVED':
                request['required_repair'] = (
                    'Reduce the number of lower-priority stops or materially reduce total dwell minutes. '
                    'Only shifting clocks or shortening the day span slightly is not adequate.')
            if previous_rejection.get('error_code') == 'REVISION_REPLACEMENT_TARGET_NOT_MET':
                request['required_repair'] = (
                    'Replace an existing stop of the specifically requested category, '
                    'not a nearby unrelated stop.')
            request['instruction'] = (
                'Repair the specific rejected edit with a NEW small operations array. '
                'Consult schedule_context occupied intervals and free gaps. '
                'A request for times AFTER a stated time means strictly later, '
                'not equal to it. If the original last stop occupies the requested '
                'time, start later, shorten/move that stop when acceptable, or '
                'replace a lower-priority stop. Never create overlaps. '
                'If the rejected proposal repeats a named venue, select a DIFFERENT '
                'venue or edit the already planned visit rather than adding it twice. '
                'If there is no usable gap, replace a lower-priority stop or shift '
                'affected start times using edit actions. Preserve all unaffected '
                'stops and independent metadata. Include necessary incoming transfers '
                'and update derived narratives. Never invent an ETA or opening hour.')
        try:
            patch = await asyncio.wait_for(
                models.complete_revision_patch(
                    ACTIVITY_PATCH_SYSTEM,
                    json.dumps(request, ensure_ascii=False, default=str),
                    day_number=day_number,
                    response_schema=activity_patch_schema(day_number, len(original['activities'])),
                    compact=compact), timeout=timeout)
            revised = merge_activity_patch(result, patch, day_number=day_number,
                                           defer_global=defer_global,
                                           allow_repeat_visit=bool(observable_intent.get('allow_repeat_visit')),
                                           auto_schedule=attempt > 0,
                                           not_before_minute=observable_intent.get('not_before_minute'))
            if enforce_intent:
                validate_for_commit(revised, previous=baseline,
                                    intent=observable_intent,
                                    protected_constraints=protected_constraints)
            return revised
        except (ModelUnavailable, TripValidationError, asyncio.TimeoutError) as exc:
            previous_issue = str(exc)
            previous_rejection = {
                'error_code': previous_issue.split(':', 1)[0][:80],
                'detail': previous_issue[:300],
            }
            conflicts = getattr(exc, 'revision_conflicts', None)
            if isinstance(conflicts, list) and conflicts:
                previous_rejection['duplicate_venues'] = conflicts[:3]
            if isinstance(exc, TripValidationError) and not isinstance(exc, ModelUnavailable):
                issues = getattr(exc, 'issues', None)
                if issues:
                    previous_rejection['error_code'] = issues[0].code
                    previous_issue += ' | ' + '; '.join(
                        f'{i.code}: {i.detail[:120]}' for i in issues[:3])
                    previous_rejection['issues'] = [
                        {'code': i.code, 'detail': i.detail[:150]}
                        for i in issues[:3]]
            # Preserve structured rejected attempts for controlled persistence
            # at the runtime boundary. Never allow diagnostics to raise again.
            if isinstance(patch, dict):
                try:
                    latest_editable_candidate = merge_activity_patch(
                        result, patch, day_number=day_number, review_mode=True,
                        allow_repeat_visit=True)
                except (TripValidationError, ValueError, TypeError, KeyError):
                    # Malformed operations must never become a working draft.
                    pass
            rejected_attempt = {'day': day_number, 'error': previous_issue[:1200],
                                'candidate_patch': patch if isinstance(patch, dict) else None}
            attempt_history.append(rejected_attempt)
            if isinstance(patch, dict):
                operations = patch.get('operations')
                if isinstance(operations, list):
                    prior_operations = [
                        {'action': op.get('action'), 'position': op.get('position'),
                         'description': str(op.get('activity', {}).get('description') or '')[:220]
                         if isinstance(op.get('activity'), dict) else '',
                         'place_name': str(op.get('activity', {}).get('place_name') or '')[:140]
                         if isinstance(op.get('activity'), dict) else '',
                         'start_time': activity_start(op['activity'])
                         if isinstance(op.get('activity'), dict) else None,
                         'duration_minutes': op['activity'].get('duration_minutes')
                         if isinstance(op.get('activity'), dict) else None,
                         'transfer_minutes': op['activity'].get('transfer_minutes')
                         if isinstance(op.get('activity'), dict) else None,
                         'transport_hint': str(op['activity'].get('transport_hint') or '')[:180]
                         if isinstance(op.get('activity'), dict) else ''}
                        for op in operations[:12] if isinstance(op, dict)
                    ]
            if isinstance(exc, (ModelUnavailable, asyncio.TimeoutError)):
                code = ('OUTPUT_TRUNCATED' if 'OUTPUT_TRUNCATED' in previous_issue else
                        'INVALID_MODEL_JSON' if 'INVALID_MODEL_JSON' in previous_issue else
                        'EMPTY_MODEL_RESPONSE' if 'EMPTY_MODEL_RESPONSE' in previous_issue else
                        'MODEL_TIMEOUT' if isinstance(exc, asyncio.TimeoutError) or
                        'TIMEOUT' in previous_issue else 'MODEL_UNAVAILABLE')
                if attempt == attempt_limit - 1 or code not in {'OUTPUT_TRUNCATED', 'INVALID_MODEL_JSON',
                                            'EMPTY_MODEL_RESPONSE'}:
                    failure = TripValidationError(f'MODEL_GENERATION_FAILED:{code}')
                    if latest_editable_candidate is not None:
                        failure.draft_candidate = latest_editable_candidate
                    raise failure from exc
            elif attempt == attempt_limit - 1:
                # Partial deltas cannot repair missing clocks in the baseline
                # reliably. Use one whole-day proposal before rejecting a
                # requested semantic or schedule change. A malformed patch
                # alone is not a reason to regenerate unrelated days.
                recoverable_error = any(token in previous_issue for token in (
                    'SCHEDULE_INVARIANT', 'ACTIVITY_TIME_ORDER',
                    'PHYSICAL_HANDOFF_TOO_SHORT', 'REVISION_ACTIVITY_TIME_CONFLICT',
                    'REVISION_TOPIC_REQUIREMENT_NOT_MET',
                    'REVISION_ADDITION_NOT_MET', 'REVISION_EARLY_TOPIC_NOT_MET',
                    'REVISION_ANCHORED_VISIT_NOT_MET', 'REVISION_PACE_NOT_IMPROVED',
                    'REVISION_TIME_REQUIREMENT_NOT_MET'))
                has_unknown_clocks = bool(context['schedule_context']['unknown_interval_positions'])
                if callable(getattr(models, 'complete_revision_day', None)) and (
                        recoverable_error or has_unknown_clocks):
                    try:
                        return await _recover_entire_day(
                            models, result, baseline, query, day_number=day_number,
                            fields=fields, preferences=preferences,
                            timeout=timeout, prior_error=previous_issue,
                            intent=observable_intent, enforce_intent=enforce_intent,
                            protected_constraints=protected_constraints)
                    except (ModelUnavailable, TripValidationError, asyncio.TimeoutError) as day_exc:
                        attempt_history.append({'day': day_number,
                                                'fallback': 'full_day',
                                                'error': str(day_exc)[:900]})
                        if isinstance(day_exc, ItineraryGateError):
                            exc = day_exc
                        previous_issue += ' | full_day: ' + str(day_exc)[:250]
                failure = TripValidationError(
                    f'REVISION_PATCH_INVALID:day={day_number}:{previous_issue[:550]}')
                failure.revision_attempts = attempt_history[-attempt_limit:]
                failure.issues = getattr(exc, 'issues', [])
                if latest_editable_candidate is not None:
                    failure.draft_candidate = latest_editable_candidate
                raise failure from exc
    raise TripValidationError('REVISION_PATCH_INVALID')
