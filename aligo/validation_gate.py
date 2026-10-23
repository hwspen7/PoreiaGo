"""Single, server-side pre-commit gate for all itinerary generation paths.

This module verifies constraints expressible from structured data. It never
invents travel durations, opening times or evidence provenance. Call it before
rendering an itinerary as accepted and again inside the durable write boundary.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .pipeline_integrity import _minute, schedule_invariants
from .planning_checks import core_day_issues
from .trip_state import TripValidationError
from .time_contract import activity_start_minute
from .revision_consistency import cross_area_zero_gap_issues, stale_revision_references


@dataclass(frozen=True)
class GateIssue:
    code: str
    day: int | None = None
    activity_index: int | None = None
    detail: str = ''

    def as_dict(self) -> dict:
        return {'code': self.code, 'day': self.day,
                'activity_index': self.activity_index, 'detail': self.detail[:240]}


class ItineraryGateError(TripValidationError):
    """Stable, structured diagnostics for a failed pre-commit invariant."""

    def __init__(self, issues: list[GateIssue]):
        self.issues = issues
        issue = issues[0]
        super().__init__(f'ITINERARY_GATE:{issue.code}:day={issue.day}:activity={issue.activity_index}')


def _positive_int(value: Any) -> bool:
    return type(value) is int and value > 0


def _interval_issues(day: dict, *, strict_timing: bool = False) -> list[GateIssue]:
    issues: list[GateIssue] = []
    activities = day.get('activities') or []
    number = day.get('day')
    starts = [activity_start_minute(a) for a in activities]
    scheduled = sum(value is not None for value in starts)
    # An unknown duration on a timed itinerary cannot certify a gap as safe.
    # A single stand-alone clock with unknown duration remains a legitimate
    # approximate itinerary; it is not described as fully schedule-verified.
    if strict_timing and scheduled >= 2:
        for idx, (start, activity) in enumerate(zip(starts, activities)):
            if start is not None and not _positive_int(activity.get('duration_minutes')):
                issues.append(GateIssue('TIMED_ACTIVITY_DURATION_UNKNOWN', number, idx + 1))
            elif start is None:
                issues.append(GateIssue('TIMED_ACTIVITY_START_UNKNOWN', number, idx + 1))
    previous_clock = None
    for idx, start in enumerate(starts):
        if start is not None:
            if previous_clock is not None and start < previous_clock:
                issues.append(GateIssue('ACTIVITY_TIME_ORDER', number, idx + 1))
            previous_clock = start
        if start is not None and _positive_int(activities[idx].get('duration_minutes')):
            if start + activities[idx]['duration_minutes'] > 1440:
                issues.append(GateIssue('ACTIVITY_EXCEEDS_DAY', number, idx + 1))
    # The same canonical primitive applies to new and legacy day objects.
    # Keep the second check for non-adjacent overlaps and transfer buffers.
    for problem in schedule_invariants(day):
        issues.append(GateIssue('SCHEDULE_INVARIANT', number, detail=problem))
    for activity_index, detail in cross_area_zero_gap_issues(day):
        issues.append(GateIssue('CROSS_AREA_TRANSFER_NO_GAP', number, activity_index, detail))
    from .schedule_integrity import visible_handoff_issues
    for activity_index, detail in visible_handoff_issues(day):
        issues.append(GateIssue('PHYSICAL_HANDOFF_TOO_SHORT', number, activity_index, detail))
    for problem in core_day_issues(day):
        if problem['code'] == 'cost_basis_conflict':
            issues.append(GateIssue('COST_BASIS_CONFLICT', number,
                                    problem['activity_index'] + 1, problem['message']))
    return issues


def _evidence_issues(document: dict, trusted_sources: dict[str, dict] | None) -> list[GateIssue]:
    """A URL supplied by a model is never proof of a verified claim.

    trusted_sources are server-owned observations keyed by URL. They must
    include a nonempty provider and a nonempty observed_at timestamp. The
    caller, not the model, must establish and supply this mapping.
    """
    issues = []
    for day in document.get('days', []):
        for idx, activity in enumerate(day.get('activities', []), 1):
            if activity.get('evidence_status') != 'verified':
                continue
            url = activity.get('evidence_url')
            source = (trusted_sources or {}).get(url)
            if not isinstance(source, dict) or not all(
                isinstance(source.get(key), str) and source[key].strip()
                for key in ('provider', 'observed_at')
            ) or source.get('verified') is not True:
                issues.append(GateIssue('UNTRUSTED_VERIFIED_EVIDENCE', day.get('day'), idx))
    return issues


def _activity_identity(activity: dict) -> tuple[str, str]:
    """Conservative observable identity; not a claim of verified place matching."""
    return (str(activity.get('place_name') or '').strip().casefold(),
            str(activity.get('description') or '').strip().casefold())


def _topic_matches(activity: dict, keyword: str) -> bool:
    """Use structured venue category first, with conservative legacy fallback."""
    category = str(activity.get('venue_category') or '').casefold().strip()
    topic = keyword.casefold().rstrip('s')
    aliases = {'museum': {'museum', 'art museum', 'history museum', 'science museum'},
               'park': {'park', 'garden', 'botanical garden'},
               'gallery': {'gallery', 'art gallery'},
               'nightlife': {'nightlife', 'evening entertainment'}}
    if category:
        return category == topic or category in aliases.get(topic, {topic})
    searchable = ' '.join(str(activity.get(field) or '') for field in
                          ('description', 'place_name')).casefold()
    return topic in searchable


def _topic_identity_count(document: dict, keyword: str) -> int:
    identities = set()
    for day in document['days']:
        for activity in day['activities']:
            if _topic_matches(activity, keyword):
                identities.add(_activity_identity(activity))
    return len(identities)


def _new_activity_candidates(document: dict, previous: dict) -> list[dict]:
    """Find true additions using a multiset of venue identities and activity records.

    Moving a dinner to a later clock time is not adding a new activity.
    Edited descriptions without a different place are not assumed to be additions.
    """
    from collections import Counter
    def identity(activity):
        place = str(activity.get('place_name') or '').strip().casefold()
        if place:
            return ('venue', place)
        description = str(activity.get('description') or '').strip().casefold()
        # Descriptions alone are tentative identities, conservative by design.
        return ('description', description)
    old = Counter(identity(a) for d in previous['days'] for a in d['activities'])
    added = []
    for day in document['days']:
        for activity in day['activities']:
            key = identity(activity)
            if old[key] > 0:
                old[key] -= 1
            else:
                added.append(activity)
    return added


def _intent_issues(document: dict, previous: dict | None, intent: dict | None) -> list[GateIssue]:
    if not intent or not previous:
        return []
    issues: list[GateIssue] = []
    changed = [day for day in document['days'] if day != previous['days'][day['day'] - 1]]
    target_days = intent.get('target_days')
    if target_days:
        allowed = set(target_days)
        for day in changed:
            if day['day'] not in allowed:
                issues.append(GateIssue('REVISION_OUTSIDE_REQUESTED_DAYS', day['day']))
    if not changed and intent.get('requires_activity_change'):
        issues.append(GateIssue('REVISION_REQUIREMENT_NOT_MET'))
    additions = _new_activity_candidates(document, previous)
    if intent.get('requires_new_activity') and not additions:
        issues.append(GateIssue('REVISION_ADDITION_NOT_MET',
            detail='The edit did not add a distinct activity. A change to an existing activity or its description alone does not satisfy an addition request.'))
    earliest = intent.get('not_before_minute')
    if earliest is not None and not any(
        (activity_start_minute(a) is not None and
         activity_start_minute(a) > earliest) for a in additions
    ):
        issues.append(GateIssue('REVISION_TIME_REQUIREMENT_NOT_MET',
            detail=f'Expected a genuinely new activity beginning strictly after '
                   f'{earliest // 60:02d}:{earliest % 60:02d}; existing activities moved later do not count.'))
    keyword = intent.get('requested_term')
    if keyword:
        before = _topic_identity_count(previous, keyword)
        after = _topic_identity_count(document, keyword)
        added_topics = [a for a in additions if _topic_matches(a, keyword)]
        if after <= before or not added_topics:
            issues.append(GateIssue('REVISION_TOPIC_REQUIREMENT_NOT_MET',
                detail=(f'Add a distinct stop for requested topic {keyword!r}. '
                        f'Qualifying stops before={before}, after={after}, '
                        f'qualifying genuinely added stops={len(added_topics)}. '
                        'Simply renaming an existing visit does not count.')))
    replacement_topic = intent.get('replace_topic')
    if replacement_topic:
        # A category-specific replacement must remove an actual matching visit.
        old_matching = {_activity_identity(a) for d in previous['days'] for a in d['activities']
                        if _topic_matches(a, replacement_topic)}
        current_matching = {_activity_identity(a) for d in document['days'] for a in d['activities']
                            if _topic_matches(a, replacement_topic)}
        if not old_matching or not (old_matching - current_matching):
            issues.append(GateIssue('REVISION_REPLACEMENT_TARGET_NOT_MET',
                                    detail=f'No existing {replacement_topic} visit was replaced.'))
    anchor_name = intent.get('anchor_place')
    anchor_minute = intent.get('anchor_minute')
    if anchor_name and type(anchor_minute) is int:
        name = ' '.join(str(anchor_name).casefold().split())
        tolerance = int(intent.get('anchor_tolerance_minutes', 0))
        def matches_anchor(activity):
            display = ' '.join(str(activity.get(k) or '') for k in
                               ('place_name', 'description')).casefold()
            at = activity_start_minute(activity)
            return name in display and at is not None and abs(at - anchor_minute) <= tolerance
        if not any(matches_anchor(a) for d in document['days'] for a in d['activities']):
            issues.append(GateIssue('REVISION_ANCHORED_VISIT_NOT_MET',
                detail=f'Add or schedule the requested visit {anchor_name!r} at '
                       f'{anchor_minute // 60:02d}:{anchor_minute % 60:02d}; '
                       'the visit must appear in the activities list.'))
    early_topic = intent.get('early_topic')
    if early_topic:
        minute = intent.get('early_target_minute')
        tolerance = intent.get('early_tolerance_minutes', 60)
        if type(minute) is int:
            # Compare visits to the requested category, not arbitrary events
            # moved to the morning or prose that merely claims an addition.
            def qualifying(doc):
                return {(a.get('place_name') or a.get('description') or '').strip().casefold()
                        for d in doc['days'] for a in d['activities']
                        if _topic_matches(a, early_topic)
                        and (start := activity_start_minute(a)) is not None
                        and max(0, minute - tolerance) <= start <= minute + tolerance}
            if not qualifying(document) - qualifying(previous):
                issues.append(GateIssue('REVISION_EARLY_TOPIC_NOT_MET',
                    detail=f'Schedule an actual {early_topic} visit near '
                           f'{minute // 60:02d}:{minute % 60:02d}; '
                           'moving another type of activity or changing notes does not count.'))
    if intent.get('less_rushed'):
        # A one-hour reduction of a 20-hour day, with all seven visits and
        # their dwell times intact, is not meaningful pace relief. Require
        # a substantive reduction in active work, not a textual pace label.
        def effort(doc):
            durations = [a.get('duration_minutes') for d in doc['days'] for a in d['activities']]
            return (sum(len(d['activities']) for d in doc['days']),
                    sum(n for n in durations if type(n) is int and n > 0))
        old_count, old_duration = effort(previous)
        new_count, new_duration = effort(document)
        recovered = old_duration - new_duration
        meaningful = (
            (new_count < old_count and recovered >= 30) or
            recovered >= max(60, int(old_duration * 0.12))
        )
        if not meaningful:
            issues.append(GateIssue('REVISION_PACE_NOT_IMPROVED',
                detail=(f'Active stops before={old_count}, after={new_count}; '
                        f'dwell minutes before={old_duration}, after={new_duration}. '
                        'Reduce lower-priority stops or substantive dwell time; '
                        'a slightly shorter clock span or a pace label alone is insufficient.')))
    return issues


def _clock_expression(text: str) -> tuple[int, int] | None:
    """Parse a simple 12/24-hour clock with optional colon or minutes."""
    match = re.search(r'(?<!\d)(\d{1,2})(?::([0-5]\d))?\s*(a\.?m\.?|p\.?m\.?)?\b', text, re.I)
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2) or 0)
    suffix = str(match.group(3) or '').lower().replace('.', '')
    if suffix:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if suffix == 'pm' else 0)
    elif hour > 23:
        return None
    return hour * 60 + minute, match.start()


def explicit_revision_intent(request: str, *, target_days: list[int] | None = None) -> dict:
    """Extract directly observable requirements without certifying semantics.

    The LLM remains responsible for intent beyond this conservative grammar.
    Recognizable requests for early visits must not silently become untested
    free-form changes, even when they do not use the verb 'add'.
    """
    if not isinstance(request, str):
        return {}
    result: dict = {'target_days': target_days or []}
    text = request.strip()
    more = re.search(r'\b(?:visit|see|tour|explore|include|add|experience)\s+'
                     r'(?:some\s+)?more\s+([A-Za-z][A-Za-z-]{2,40})\b', text, re.I)
    adding = bool(re.search(r'\b(?:add|include|additional|extra)\b', text, re.I)) or bool(more)
    if adding:
        result['requires_activity_change'] = True
        result['requires_new_activity'] = True
        if more:
            term = more.group(1).casefold()
            if term.endswith('s') and not term.endswith('ss'):
                term = term[:-1]
            if term not in {'time', 'day', 'hour', 'activity', 'activitie', 'thing', 'option'}:
                result['requested_term'] = term
        after = re.search(r'\bafter\s+(\d{1,2}(?::[0-5]\d)?\s*(?:a\.?m\.?|p\.?m\.?)?)', text, re.I)
        if after:
            parsed = _clock_expression(after.group(1))
            if parsed:
                result['not_before_minute'] = parsed[0]
    # A morning visit can be achieved by moving an existing qualifying stop
    # or adding one if absent. The category is checked against real activities.
    early = re.search(r'\b(?:earlier|early|morning|before\s+\d{1,2}|around\s+\d{1,2})\b', text, re.I)
    topic = re.search(r'\b(?:visit|see|tour|explore|include|add|experience)\s+'
                      r'(?:some|a|the|more|additional|few|several|of\s+the)*\s*'
                      r'(parks?|gardens?|museums?|galleries?)\b', text, re.I)
    if early and topic:
        word = topic.group(1).casefold().rstrip('s')
        result['requires_activity_change'] = True
        result['early_topic'] = 'garden' if word == 'garden' else word
        exact_clock = re.search(r'\b(?:around|at|like|near|about|by)\s+'
                                r'(\d{1,2}(?::[0-5]\d)?\s*(?:a\.?m\.?|p\.?m\.?)?)', text, re.I)
        parsed = _clock_expression(exact_clock.group(1)) if exact_clock else None
        if parsed:
            result['early_target_minute'] = parsed[0]
            result['early_tolerance_minutes'] = 60 if re.search(r'\b(?:around|like|near|about)\b', text, re.I) else 30
        elif re.search(r'\bmorning\b', text, re.I):
            result['early_target_minute'] = 8 * 60
            result['early_tolerance_minutes'] = 180
    anchor = re.search(r'\b(?:go to|visit|see|stop at|arrive at)\s+'
                       r'(?:the\s+)?([A-Za-z][A-Za-z\s\'-]{2,80}?)\s+'
                       r'(at|around|about)\s+'
                       r'(\d{1,2}(?::[0-5]\d)?\s*(?:a\.?m\.?|p\.?m\.?)?)\b', text, re.I)
    if anchor:
        parsed = _clock_expression(anchor.group(3))
        if parsed:
            result['requires_activity_change'] = True
            result['anchor_place'] = anchor.group(1).strip()
            result['anchor_minute'] = parsed[0]
            result['anchor_tolerance_minutes'] = 30 if anchor.group(2).lower() != 'at' else 0
    removal = re.search(
        r'\b(?:remove|delete|drop|skip|exclude|cancel)\s+(?:the\s+)?'
        r'(.+?)(?:\s+from\s+(?:my\s+|the\s+|this\s+)?'
        r'(?:trip|plan|schedule|itinerary)|[.!?]|$)', text, re.I)
    if removal:
        result['remove_place'] = removal.group(1).strip()[:120]
        result['requires_activity_change'] = True
    replacing = re.search(r'\breplace\s+(?:one|a|the|some)?\s*([a-z][a-z-]{2,30})\s+with\b', text, re.I)
    if replacing:
        result['requires_activity_change'] = True
        term = replacing.group(1).casefold().rstrip('s')
        if term not in {'activity', 'stop', 'visit', 'thing'}:
            result['replace_topic'] = term
    if re.search(r'\b(?:less rushed|less packed|less hectic|more relaxed|reduce activities)\b', text, re.I):
        result['requires_activity_change'] = True
        result['less_rushed'] = True
    if re.search(r'\b(?:again|revisit|repeat visit|visit twice)\b', text, re.I):
        result['allow_repeat_visit'] = True
    return result


def unresolved_schedule_fields(document: dict) -> list[GateIssue]:
    """Report incomplete timing as unknown, never assume a zero-minute stop."""
    issues = []
    for day in document.get('days', []):
        activities = day.get('activities', [])
        starts = [activity_start_minute(a) for a in activities]
        if sum(start is not None for start in starts) < 2:
            continue
        for idx, (start, activity) in enumerate(zip(starts, activities), 1):
            if start is None:
                issues.append(GateIssue('ACTIVITY_START_UNVERIFIED', day.get('day'), idx))
            elif not _positive_int(activity.get('duration_minutes')):
                issues.append(GateIssue('ACTIVITY_DURATION_UNVERIFIED', day.get('day'), idx))
    return issues


def _cross_day_revision_issues(document: dict, previous: dict | None,
                               intent: dict | None) -> list[GateIssue]:
    """Check certain identity-level conflicts introduced by an edit.

    Only explicit place_name + base_city pairs qualify. District similarity,
    fuzzy venue names, and hotel transfer times are never guessed. Existing
    duplicated stops are grandfathered until one is edited or reintroduced.
    """
    if not previous or len(document.get('days', [])) < 2:
        return []
    if (intent or {}).get('allow_repeat_visit'):
        return []

    def visits(day: dict) -> set[tuple[str, str]]:
        city = ' '.join(str(day.get('base_city') or '').casefold().split())
        if not city:
            return set()
        found = set()
        for activity in day.get('activities', []):
            if activity.get('activity_type') in {'meal', 'lodging', 'transport', 'rest'}:
                continue
            name = ' '.join(str(activity.get('place_name') or '').casefold().split())
            if len(name) >= 5:
                found.add((city, name))
        return found

    old_visits = {d['day']: visits(d) for d in previous['days']}
    new_visits = {d['day']: visits(d) for d in document['days']}
    before = {name for day, entries in old_visits.items() for name in entries
              if any(name in other for other_day, other in old_visits.items()
                     if other_day != day)}
    issues = []
    for day in document['days']:
        number = day['day']
        added = new_visits[number] - old_visits[number]
        for identity in added:
            if identity not in before and any(
                    identity in names for other, names in new_visits.items()
                    if other != number):
                issues.append(GateIssue('CROSS_DAY_DUPLICATE_NAMED_VENUE', number,
                                        detail='Repeated explicit venue: ' + identity[1][:120]))
    return issues


def validate_for_commit(document: dict, *, previous: dict | None = None,
                        intent: dict | None = None,
                        trusted_sources: dict[str, dict] | None = None,
                        strict_timing: bool = False,
                        
                 protected_constraints=None, constraint_intents=None) -> dict:
    """Fail closed before saving. Does not mutate the caller's document."""
    from .trip_state import validate_itinerary
    # Validate shape once even when a caller bypasses the normal planner.
    normalized = validate_itinerary(document, operation='create')
    issues = []
    for day in normalized['days']:
        issues.extend(_interval_issues(day, strict_timing=strict_timing))
    issues.extend(_evidence_issues(normalized, trusted_sources))
    issues.extend(_cross_day_revision_issues(normalized, previous, intent))
    from .pipeline_integrity import transition_invariants
    for i in range(1, len(normalized['days'])):
        old, new = normalized['days'][i - 1:i + 1]
        prior_base = str(old.get('overnight_base') or '').strip()
        current_base = str(new.get('overnight_base') or '').strip()
        from_city = str(old.get('base_city') or '').strip()
        to_city = str(new.get('base_city') or '').strip()
        # Missing bases do not prove a hotel move. Require explicit different
        # overnight allocations before invoking strict transition checks.
        if (not (prior_base and current_base and from_city and to_city) or
                prior_base.casefold() == current_base.casefold()):
            continue
        for detail in transition_invariants(new, previous_day=old,
            previous_allocation={'city': from_city, 'overnight': prior_base},
            allocation={'city': to_city, 'overnight': current_base}):
            issues.append(GateIssue('CROSS_DAY_TRANSFER_INCOMPLETE', new['day'],
                                    1, str(detail)[:240]))
    for day_number, activity_index, field in stale_revision_references(normalized, previous):
        issues.append(GateIssue('STALE_REVISION_REFERENCE', day_number, activity_index,
                                'Outdated routing or summary field: ' + field))
    issues.extend(_intent_issues(normalized, previous, intent))
    from .journey_constraints import unresolved_requirements
    for entry in unresolved_requirements(normalized, protected_constraints, intent,
                                         superseding_intents=constraint_intents):
        issues.append(GateIssue(entry['code'], detail=entry['detail']))
    if issues:
        raise ItineraryGateError(issues)
    return normalized
