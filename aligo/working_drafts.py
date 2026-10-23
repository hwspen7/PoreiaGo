"""Durable draft review with explicit, traveler-visible unresolved issues."""
from .trip_state import validate_itinerary
from .validation_gate import validate_for_commit, ItineraryGateError, unresolved_schedule_fields


def review_draft(document, *, previous=None, intent=None, protected_constraints=None, constraint_intents=None):
    candidate = validate_itinerary(document, operation='create')
    issues = []
    # A model's verified label has no evidentiary authority. Revoke it before
    # invoking the validation gate, rather than telling the traveler it is a
    # problem and then silently resolving it afterward.
    for day in candidate['days']:
        for activity in day['activities']:
            if activity.get('evidence_status') == 'verified':
                activity['evidence_status'] = 'unverified'
                if not activity.get('verification_needed'):
                    activity['verification_needed'] = 'Verify this place or claim with an authoritative source.'
    from .time_contract import activity_start_minute
    for day in candidate['days']:
        activities = day['activities']
        starts = [activity_start_minute(item) for item in activities]
        if any(a is not None and b is not None and a > b
               for a, b in zip(starts, starts[1:])):
            previous_order = {id(row): i for i, row in enumerate(activities)}
            activities.sort(key=lambda item: (
                activity_start_minute(item) is None,
                activity_start_minute(item) if activity_start_minute(item) is not None else 1440))
            for index, activity in enumerate(activities):
                old_index = previous_order[id(activity)]
                old_predecessor = previous_order[id(activities[index - 1])] if index else None
                if old_index > 0 and old_predecessor != old_index - 1:
                    activity.pop('transport_hint', None)
                    activity.pop('transfer_minutes', None)
                    activity.pop('buffer_minutes', None)
                    if not activity.get('verification_needed'):
                        activity['verification_needed'] = 'Recheck inbound routing after chronological reorder.'
            issues.append({'code': 'DRAFT_ACTIVITY_ORDER_RECONCILED',
                           'day': day['day'],
                           'detail': 'Activities were sorted chronologically. Review transfers and dependencies.'})
        if len(activities) > 1 and not any(activity_start_minute(a) is not None for a in activities):
            issues.append({'code': 'DRAFT_CLOCKS_UNSPECIFIED', 'day': day['day'],
                           'detail': 'Start times are approximate dayparts; exact scheduling remains unresolved.'})
    try:
        validate_for_commit(candidate, previous=previous, intent=intent, protected_constraints=protected_constraints,
                            constraint_intents=constraint_intents)
    except ItineraryGateError as exc:
        issues.extend(issue.as_dict() for issue in exc.issues)
    issues.extend(issue.as_dict() for issue in unresolved_schedule_fields(candidate))
    from .revision_consistency import duplicate_stop_conflicts
    if not (intent or {}).get('allow_repeat_visit'):
        for day in candidate['days']:
            rows = day['activities']
            for conflict in duplicate_stop_conflicts(rows, [None] * len(rows)):
                issues.append({'code': 'DRAFT_DUPLICATE_NAMED_VENUE', 'day': day['day'],
                    'activity_index': conflict['proposed_position'],
                    'detail': f"Repeated venue also at activity {conflict['existing_position']}: "
                              + conflict['matched_alias']})
    # Collapse duplicate diagnostic entries that emerged from independent
    # checks and show actionable schedule problems before optional metadata.
    unique = {}
    for issue in issues:
        signature = (issue.get('code'), issue.get('day'), issue.get('activity_index'), issue.get('detail'))
        unique.setdefault(signature, issue)
    return candidate, list(unique.values())


def draft_preface(draft):
    lines = [f"Working draft {draft['draft_version']} saved. You can continue editing this plan."]
    issues = draft.get('issues') or []
    if issues:
        lines.append('Review needed:')
        for issue in issues[:8]:
            location = f"Day {issue['day']}" if issue.get('day') else 'Trip'
            if issue.get('activity_index'):
                location += f", activity {issue['activity_index']}"
            lines.append(f"- {location}: {issue.get('detail') or issue.get('code', 'Review required')}")
        if len(issues) > 8:
            lines.append(f"- {len(issues) - 8} additional issues are listed in the draft review.")
    lines.append('Saved status does not verify opening hours, ticket availability or event times.')
    return '\n'.join(lines)


def saved_response(saved, session_id):
    """Return one stable API contract for saved snapshots and legacy callers."""
    snapshot = saved.get('saved_snapshot')
    active = saved.get('active_itinerary')
    draft = saved.get('working_draft')
    if snapshot:
        provisional = snapshot['source_status'] == 'draft'
        kind = 'working draft' if provisional else 'verified itinerary'
        answer = (f"Saved {kind} as snapshot #{snapshot['saved_id']}. "
                  'You can continue editing this plan. ' +
                  (draft_preface(draft) if provisional and draft else
                   'Prices, opening hours and availability still require verification.'))
    else:
        answer = (draft_preface(draft) if draft else
                  f"Itinerary version {active['version']} is saved. You can continue editing it.")
    return {**saved, 'answer': answer, 'session_id': session_id,
            'results': [], 'planner_mode': 'saved_state', 'trip_change': None,
            'model_issue': None, 'model_diagnostic': None}
