"""Durable draft review with explicit, traveler-visible unresolved issues."""
from .trip_state import validate_itinerary
from .validation_gate import validate_for_commit, ItineraryGateError, unresolved_schedule_fields


def review_draft(document, *, previous=None, intent=None):
    candidate = validate_itinerary(document, operation='create')
    issues = []
    # Display chronologically without pretending that a sort repairs timing
    # conflicts. On subsequent edits, positions refer to this saved order.
    from .time_contract import activity_start_minute
    for day in candidate['days']:
        activities = day['activities']
        starts = [activity_start_minute(item) for item in activities]
        if any(a is not None and b is not None and a > b
               for a, b in zip(starts, starts[1:])):
            activities.sort(key=lambda item: (
                activity_start_minute(item) is None,
                activity_start_minute(item) if activity_start_minute(item) is not None else 1440))
            issues.append({'code': 'DRAFT_ACTIVITY_ORDER_RECONCILED',
                           'day': day['day'],
                           'detail': 'Activities were displayed in chronological order. Recheck dependent transfers and gaps.'})
    try:
        validate_for_commit(candidate, previous=previous, intent=intent)
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
    # Drafts may preserve uncertain claims, but must never retain a false
    # assertion that server-owned evidence has verified them.
    for day in candidate['days']:
        for activity in day['activities']:
            if activity.get('evidence_status') == 'verified':
                activity['evidence_status'] = 'unverified'
    return candidate, issues


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
