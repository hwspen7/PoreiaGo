"""Model-selected, activity-level itinerary deltas with lossless deterministic merges.

Model output contains only changes to existing activities. Every operation refers to
an original, one-based activity position, so a multi-operation patch has stable
indices. Full itinerary and unaffected activity records never leave the server.
"""
from __future__ import annotations

import re
from copy import deepcopy

from planning.trip_state import TripValidationError, validate_itinerary
from planning.validation_gate import validate_for_commit, ItineraryGateError
from planning.revision_consistency import reconcile_dependencies, duplicate_stop_conflicts


ACTIVITY_PATCH_SYSTEM = """You are PoreiaGo's multilingual, specialist itinerary edit planner.
Output ONLY JSON containing a day number and a small operations array. Never
rewrite the full day, prior activities, another day, or the entire itinerary.
Use ORIGINAL activity positions (1-based):
- insert: position 0 to insert before the first activity, or 1..N to insert
  immediately AFTER original activity at that position.
- edit: position 1..N, include ONLY the activity fields that must change.
- replace: position 1..N, provide the complete new activity.
- remove: position 1..N; omit activity.
All unchanged activities and their metadata are preserved automatically.
For every insert or replace on a timed itinerary, the activity object MUST
contain description, start_time as a precise 24-hour HH:MM clock, and
integer duration_minutes. A phrase such as "after 8pm" is a CONSTRAINT,
not a start_time. Choose an actual precise time within a feasible clock gap.
For an edit that changes the time, use start_time HH:MM explicitly.
For additions, choose feasible visit times and adjust affected existing stop
start times by edit operations where needed. When there is no time available,
replace lower-priority visits instead of creating overlapping activities.
Preserve user constraints, activity identity and specific reliable evidence.
If a stop is replaced or removed, update dependent later activities, day
logistics and trip summaries in the SAME patch where necessary. Do not leave
references to departed venues, former inbound transit, or obsolete admissions.
Include day_updates or trip_updates for derived copy as needed. A new venue
with a different district needs a real transfer gap; never invent travel time.
When an insertion does not fit, prefer a coherent replacement or reschedule,
not an overlap. Only update metadata that needs changing.
Provide venue_category where known (e.g. museum, gallery, park, nightlife).
Use the user's language and grounded places.
Do not invent verified opening hours, prices or bookings; mark unknown when needed.
Do not add meal breaks unless the user's requests/pace imply them.
Do not output markdown, explanations, internal reasoning or extra keys.
"""


_ACTIVITY_FIELDS = {
    'description': {'type': 'string', 'maxLength': 500},
    'start_time': {'type': 'string', 'pattern': r'^([01][0-9]|2[0-3]):[0-5][0-9]$'},
    'time': {'type': 'string', 'maxLength': 70},
    'duration_minutes': {'type': 'integer', 'minimum': 10, 'maximum': 720},
    'transfer_minutes': {'type': 'integer', 'minimum': 0, 'maximum': 360},
    'buffer_minutes': {'type': 'integer', 'minimum': 0, 'maximum': 180},
    'why_this_fits': {'type': 'string', 'maxLength': 260},
    'transport_hint': {'type': 'string', 'maxLength': 180},
    'verification_needed': {'type': 'string', 'maxLength': 260},
    'booking_advice': {'type': 'string', 'maxLength': 260},
    'area': {'type': 'string', 'maxLength': 120},
    'place_name': {'type': 'string', 'maxLength': 140},
    'venue_category': {'type': 'string', 'maxLength': 60},
    'activity_type': {'type': 'string', 'enum': ['attraction', 'meal', 'transport', 'lodging', 'experience', 'rest', 'shopping', 'other']},
    'meal_type': {'type': 'string', 'enum': ['breakfast', 'lunch', 'dinner', 'snack']},
    'cost_estimate': {'type': 'string', 'maxLength': 120},
    'cost_level': {'type': 'string', 'enum': ['free', 'low', 'moderate', 'high', 'unknown']},
    'cost_currency': {'type': 'string', 'maxLength': 6},
    'cost_basis': {'type': 'string', 'enum': ['per_person', 'per_group', 'per_item', 'unknown']},
    'evidence_status': {'type': 'string', 'enum': ['unverified', 'pending', 'verified']},
    'evidence_url': {'type': 'string', 'maxLength': 320},
    'evidence_title': {'type': 'string', 'maxLength': 160},
}
_TRIP_FIELDS = {
    'title': {'type': 'string', 'maxLength': 180},
    'summary': {'type': 'string', 'maxLength': 1200},
    'budget_strategy': {'type': 'string', 'maxLength': 600},
    'arrival_departure_plan': {'type': 'string', 'maxLength': 500},
}
_DAY_FIELDS = {
    'title': {'type': 'string', 'maxLength': 180},
    'notes': {'type': 'string', 'maxLength': 900},
    'theme': {'type': 'string', 'maxLength': 140},
    'area_cluster': {'type': 'string', 'maxLength': 160},
    'base_city': {'type': 'string', 'maxLength': 140},
    'overnight_base': {'type': 'string', 'maxLength': 180},
    'intercity_transfer': {'type': 'string', 'maxLength': 260},
    'logistics': {'type': 'string', 'maxLength': 360},
    'weather_alternative': {'type': 'string', 'maxLength': 320},
    'budget_note': {'type': 'string', 'maxLength': 260},
    'pace': {'type': 'string', 'enum': ['light', 'balanced', 'full']},
}


def activity_patch_schema(day_number: int, original_count: int) -> dict:
    """Small JSON schema independent of the existing day output size."""
    if not 1 <= original_count <= 12:
        raise ValueError('Original day must contain 1 to 12 activities.')
    return {
        'type': 'object',
        'properties': {
            'day': {'type': 'integer', 'enum': [day_number]},
            'operations': {'type': 'array', 'minItems': 1, 'maxItems': 12,
                'items': {'type': 'object', 'properties': {
                    'action': {'type': 'string', 'enum': ['insert', 'edit', 'replace', 'remove']},
                    'position': {'type': 'integer', 'minimum': 0, 'maximum': original_count},
                    'activity': {'type': 'object', 'properties': deepcopy(_ACTIVITY_FIELDS),
                                 'additionalProperties': False},
                }, 'required': ['action', 'position'], 'additionalProperties': False}},
            'day_updates': {'type': 'object', 'properties': deepcopy(_DAY_FIELDS),
                            'additionalProperties': False},
            'trip_updates': {'type': 'object', 'properties': deepcopy(_TRIP_FIELDS),
                             'additionalProperties': False},
        }, 'required': ['day', 'operations'], 'additionalProperties': False,
    }


def _clock(value: object) -> int | None:
    if not isinstance(value, str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', value):
        return None
    hours, minutes = map(int, value.split(':'))
    return hours * 60 + minutes


_VENUE_DEPENDENT_FIELDS = frozenset({
    'why_this_fits', 'transport_hint', 'transfer_minutes', 'buffer_minutes',
    'booking_advice', 'verification_needed', 'cost_estimate', 'cost_level',
    'cost_currency', 'cost_basis', 'evidence_status', 'evidence_url',
    'evidence_title',
})
_TIME_DEPENDENT_FIELDS = frozenset({
    'transport_hint', 'transfer_minutes', 'buffer_minutes', 'booking_advice',
    'verification_needed', 'evidence_status', 'evidence_url', 'evidence_title',
})


def _merge_edit_without_stale_facts(existing: dict, changes: dict) -> dict:
    """Keep unrelated data and discard stale facts when their inputs change."""
    updated = deepcopy(existing)
    venue_changed = any(
        key in changes and changes[key] != existing.get(key)
        for key in ('description', 'place_name', 'area', 'activity_type'))
    time_changed = any(
        key in changes and changes[key] != existing.get(key)
        for key in ('start_time', 'time', 'duration_minutes'))
    invalidated = (_VENUE_DEPENDENT_FIELDS if venue_changed else
                   _TIME_DEPENDENT_FIELDS if time_changed else frozenset())
    for key in invalidated:
        # Explicitly supplied new data remains unverified until server-owned
        # evidence establishes trust in the shared precommit gate.
        updated.pop(key, None)
    updated.update(changes)
    if invalidated and 'evidence_status' not in changes:
        updated['evidence_status'] = 'pending'
    if 'time' in changes and 'start_time' not in changes:
        updated.pop('start_time', None)
    if 'start_time' in changes and 'time' not in changes:
        updated.pop('time', None)
    return updated


def merge_activity_patch(baseline: dict, patch: dict, *, day_number: int,
                         defer_global: bool = False,
                         allow_repeat_visit: bool = False,
                         review_mode: bool = False,
                         auto_schedule: bool = False,
                         not_before_minute: int | None = None) -> dict:
    """Apply an untrusted patch to a copy of the saved itinerary or raise.

    All indices in the operations list refer to the unchanged original day.
    Validation is performed once on the complete merged document, then a
    scheduling check focuses on newly edited or inserted activities.
    """
    if not isinstance(patch, dict) or type(patch.get('day')) is not int or patch['day'] != day_number:
        raise TripValidationError('REVISION_PATCH_WRONG_DAY')
    days = baseline.get('days')
    if not isinstance(days, list) or not 1 <= day_number <= len(days):
        raise TripValidationError('REVISION_PATCH_WRONG_DAY')
    original_day = days[day_number - 1]
    originals = original_day['activities']
    operations = patch.get('operations')
    if not isinstance(operations, list) or not 1 <= len(operations) <= 12:
        raise TripValidationError('REVISION_PATCH_INVALID_OPERATIONS')
    inserts: dict[int, list[dict]] = {}
    updates: dict[int, tuple[str, dict]] = {}
    for operation in operations:
        if not isinstance(operation, dict) or set(operation) - {'action', 'position', 'activity'}:
            raise TripValidationError('REVISION_PATCH_INVALID_OPERATION')
        action, position = operation.get('action'), operation.get('position')
        activity = operation.get('activity')
        if action not in {'insert', 'edit', 'replace', 'remove'} or type(position) is not int:
            raise TripValidationError('REVISION_PATCH_INVALID_OPERATION')
        if (position < (0 if action == 'insert' else 1) or position > len(originals)):
            raise TripValidationError('REVISION_PATCH_INVALID_POSITION')
        if action == 'remove':
            if activity is not None:
                raise TripValidationError('REVISION_PATCH_UNEXPECTED_ACTIVITY')
            if position in updates:
                raise TripValidationError('REVISION_PATCH_CONFLICTING_OPERATIONS')
            updates[position] = (action, {})
            continue
        if not isinstance(activity, dict) or not activity or set(activity) - _ACTIVITY_FIELDS.keys():
            raise TripValidationError('REVISION_PATCH_INVALID_ACTIVITY')
        if action in {'insert', 'replace'} and not isinstance(activity.get('description'), str):
            raise TripValidationError('REVISION_PATCH_MISSING_DESCRIPTION')
        # A precisely timed baseline cannot accept a new stop whose duration
        # is unknowable. Do not silently certify time feasibility by skipping it.
        from planning.time_contract import activity_start_minute
        scheduled_before = sum(activity_start_minute(a) is not None for a in originals)
        if action == 'replace' and scheduled_before >= 2:
            old = originals[position - 1]
            if activity_start_minute(activity) is None:
                previous_start = activity_start_minute(old)
                if previous_start is not None:
                    activity = deepcopy(activity)
                    activity['start_time'] = f'{previous_start // 60:02d}:{previous_start % 60:02d}'
            if activity.get('duration_minutes') is None and type(old.get('duration_minutes')) is int:
                activity = deepcopy(activity)
                activity['duration_minutes'] = old['duration_minutes']
        if action in {'insert', 'replace'} and scheduled_before >= 2 and not review_mode:
            if activity_start_minute(activity) is None:
                raise TripValidationError(
                    f'REVISION_NEW_ACTIVITY_MISSING_START_TIME: action={action}; original_position={position}; '
                    'supply activity.start_time in 24-hour HH:MM; a vague period is insufficient')
            if type(activity.get('duration_minutes')) is not int:
                raise TripValidationError(
                    f'REVISION_NEW_ACTIVITY_MISSING_DURATION: action={action}; original_position={position}; '
                    'supply a positive integer activity.duration_minutes')

        if action == 'edit' and ('duration_minutes' in activity and
                                 activity['duration_minutes'] is None):
            raise TripValidationError('REVISION_CANNOT_REMOVE_KNOWN_DURATION')
        if action == 'insert':
            inserts.setdefault(position, []).append(activity)
        else:
            if position in updates:
                raise TripValidationError('REVISION_PATCH_CONFLICTING_OPERATIONS')
            updates[position] = (action, activity)
    if len(originals) + sum(map(len, inserts.values())) - sum(
            action == 'remove' for action, _ in updates.values()) > 12:
        raise TripValidationError('REVISION_TOO_MANY_ACTIVITIES')
    if len(originals) - sum(action == 'remove' for action, _ in updates.values()) + sum(map(len, inserts.values())) < 1:
        raise TripValidationError('REVISION_DAY_EMPTY')
    day = deepcopy(original_day)
    result_activities = []
    origins: list[int | None] = []
    changed_originals: set[int] = set()
    explicit_updates: dict[int, set[str]] = {}
    def append_new(activity: dict) -> None:
        result_activities.append(deepcopy(activity))
        origins.append(None)

    for activity in inserts.get(0, []):
        append_new(activity)
    for number, existing in enumerate(originals, 1):
        operation = updates.get(number)
        if operation is None:
            result_activities.append(deepcopy(existing))
            origins.append(number)
        elif operation[0] == 'replace':
            changed_originals.add(number)
            append_new(operation[1])
        elif operation[0] == 'edit':
            explicit_updates[number] = set(operation[1])
            if any(key in operation[1] and operation[1][key] != existing.get(key)
                   for key in ('description', 'place_name', 'area', 'start_time', 'duration_minutes')):
                changed_originals.add(number)
            result_activities.append(_merge_edit_without_stale_facts(existing, operation[1]))
            origins.append(number)
        if operation is not None and operation[0] == 'remove':
            changed_originals.add(number)
        for activity in inserts.get(number, []):
            append_new(activity)
    if set(patch) - {'day', 'operations', 'day_updates', 'trip_updates'}:
        raise TripValidationError('REVISION_PATCH_UNEXPECTED_FIELDS')
    metadata = patch.get('day_updates', {})
    if not isinstance(metadata, dict) or set(metadata) - _DAY_FIELDS.keys():
        raise TripValidationError('REVISION_PATCH_INVALID_DAY_UPDATES')
    trip_updates = patch.get('trip_updates', {})
    if not isinstance(trip_updates, dict) or set(trip_updates) - _TRIP_FIELDS.keys():
        raise TripValidationError('REVISION_PATCH_INVALID_TRIP_UPDATES')
    day.update(metadata)
    day['activities'] = result_activities
    validated = validate_itinerary({'changes': [day], **trip_updates},
                                   previous=baseline, operation='revise')
    validated = reconcile_dependencies(baseline, validated, day_number=day_number,
                                       origins=origins, changed_originals=changed_originals,
                                       explicit_updates=explicit_updates)
    if auto_schedule:
        from planning.revision_resilience import reconcile_schedule
        adjusted_day, origins = reconcile_schedule(validated['days'][day_number - 1], origins,
                                                    not_before_minute=not_before_minute)
        validated['days'][day_number - 1] = adjusted_day
    conflicts = ([] if allow_repeat_visit else duplicate_stop_conflicts(
        validated['days'][day_number - 1]['activities'], origins,
        changed_originals=changed_originals))
    if conflicts and not review_mode:
        conflict = conflicts[0]
        issue = TripValidationError(
            'REVISION_DUPLICATE_NAMED_VENUE: proposed position '
            f"{conflict['proposed_position']} duplicates position "
            f"{conflict['existing_position']} (alias: "
            f"{conflict['matched_alias']}).")
        issue.revision_conflicts = conflicts[:3]
        raise issue
    if defer_global or review_mode:
        return validated
    try:
        validate_for_commit(validated, previous=baseline)  # Scan all days and transitions.
    except ItineraryGateError as exc:
        if any(issue.code in {'ACTIVITY_TIME_ORDER', 'ACTIVITY_EXCEEDS_DAY', 'SCHEDULE_INVARIANT',
                                   'CROSS_AREA_TRANSFER_NO_GAP'}
               for issue in exc.issues):
            raise TripValidationError(
                'REVISION_ACTIVITY_TIME_CONFLICT:' +
                ','.join(f'{issue.code}@day{issue.day}/stop{issue.activity_index}: {issue.detail[:130]}'
                         for issue in exc.issues[:3])) from exc
        if any(issue.code == 'TIMED_ACTIVITY_START_UNKNOWN' for issue in exc.issues):
            raise TripValidationError('REVISION_NEW_ACTIVITY_MISSING_START_TIME') from exc
        raise
    if validated['days'][day_number - 1] == original_day:
        raise TripValidationError('REVISION_PATCH_NO_EFFECT')
    return validated
