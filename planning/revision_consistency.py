"""Dependency-aware reconciliation and conservative itinerary transition checks.

A changed stop invalidates derived narrative and inbound movement details that
refer to the former stop. Unknown route durations remain unknown. The planner
may supply replacement details in the same compact activity patch.
"""
from __future__ import annotations

import re
from copy import deepcopy


_VENUE = re.compile(
    r"\b(?i:visit|explore|tour|see|enter|discover|walk through|experience)\s+"
    r"(?:the\s+)?((?:[A-Z][A-Za-z'-]*|of|the|and)"
    r"(?:\s+(?:[A-Z][A-Za-z'-]*|of|the|and)){1,5})\b"
)
_PREVIOUS_REFERENCE = re.compile(
    r"\b(?:you (?:just|previously) (?:visited|explored|walked|saw)|"
    r"just (?:visited|explored|walked through)|"
    r"after (?:visiting|exploring)|from (?:the )?previous stop|"
    r"the (?:previous|last) (?:stop|attraction))\b", re.I
)
_DEPARTURE_COPY = re.compile(r"\b(?:starts?|begins?|concludes?|ends?|finishes?)\s+(?:with|at|in)\b", re.I)
_ADMIN_AREA = re.compile(r"\b(?:district|borough|neighbou?rhood|ward)\b", re.I)
_TRAVEL_LEG = re.compile(r"\b(?:taxi|didi|rideshare|private car|drive|driving|subway|metro|train|bus|ferry|flight|transfer|return to|travel to)\b", re.I)


# Lightweight, language-neutral-in-concept aliases from proper-name spans.
# These are only conservative hints: matching a name is not a provider identity.
_NAMED_PHRASE = re.compile(
    r"\b[A-Z][A-Za-z'-]+(?:\s+(?:(?:of|the|and|for|de|la)\s+)?"
    r"[A-Z][A-Za-z'-]+)+\b"
)
_PARENTHETICAL = re.compile(r"\(([^()]{3,100})\)")


def _normalized_alias(value: str) -> str:
    return ' '.join(re.sub(r"[^\w\s'-]", ' ', value).casefold().split())


def _named_phrases(value: str) -> set[str]:
    # Restrict hints to a single opening clause; explanatory prose often
    # references *other* sights without presenting them as stops.
    intro = str(value or '').split('.', 1)[0][:350]
    intro = re.sub(
        r'^(?i:visit|explore|tour|see|enter|discover|walk through|experience|'
        r'excursion to|day trip to|hike to)\s+(?:the\s+)?', '', intro)
    return {_normalized_alias(match.group()) for match in _NAMED_PHRASE.finditer(intro)
            if len(_normalized_alias(match.group())) >= 7}


def _primary_aliases(activity: dict) -> set[str]:
    aliases = set()
    for name in (activity.get('place_name'), _anchor(activity)):
        if name:
            aliases.add(_normalized_alias(str(name)))
    # Parentheses immediately after the named attraction often carry its
    # alternative name. Later parentheses may merely describe geography.
    description = str(activity.get('description') or '')
    first_clause = description.split('.', 1)[0][:180]
    match = _PARENTHETICAL.search(first_clause)
    if match and _named_phrases(match.group(1)):
        aliases |= _named_phrases(match.group(1))
    return {alias for alias in aliases if len(alias) >= 7}


def _former_venue_aliases(activity: dict) -> set[str]:
    # Used for invalidating copy, not as authoritative entity resolution.
    return _primary_aliases(activity) | _named_phrases(activity.get('description') or '')


def duplicate_stop_conflicts(activities: list[dict],
                             origins: list[int | None],
                             changed_originals: set[int] | None = None) -> list[dict]:
    """Describe exact named-venue duplicates with both positions and identities.

    Aliases are conservative hints, not external place identity. Diagnostics
    include enough context to let a model repair a rejected patch without
    guessing which of the existing activities caused the conflict.
    """
    conflicts: list[dict] = []
    for index, activity in enumerate(activities):
        if (origins[index] is not None and
                origins[index] not in (changed_originals or set())):
            continue
        aliases = _primary_aliases(activity)
        if not aliases:
            continue
        for other_index, other in enumerate(activities):
            if other_index == index:
                continue
            overlapping = sorted(aliases & _primary_aliases(other))
            if overlapping:
                conflicts.append({
                    'proposed_position': index + 1,
                    'existing_position': other_index + 1,
                    'matched_alias': overlapping[0][:120],
                    'proposed_description': str(activity.get('description') or '')[:220],
                    'existing_description': str(other.get('description') or '')[:220],
                    'proposed_place_name': str(activity.get('place_name') or '')[:140],
                    'existing_place_name': str(other.get('place_name') or '')[:140],
                })
                break
    return conflicts


def duplicate_new_stops(activities: list[dict], origins: list[int | None]) -> list[int]:
    """Compatibility interface for callers needing new-stop positions only."""
    return [row['proposed_position']
            for row in duplicate_stop_conflicts(activities, origins)]


def _anchor(activity: dict) -> str:
    explicit = str(activity.get('place_name') or '').strip()
    if explicit:
        return explicit
    match = _VENUE.search(str(activity.get('description') or ''))
    return re.sub(r'\s+(?:and|of|the)$', '', match.group(1).strip(), flags=re.I) if match else ''


def _mentions(text: str, phrase: str) -> bool:
    if len(phrase) < 6:
        return False
    return bool(re.search(r'(?<!\w)' + re.escape(phrase) + r'(?!\w)', text, re.I))


def _same_venue(old: dict, new: dict) -> bool:
    old_name, new_name = _anchor(old).casefold(), _anchor(new).casefold()
    if old_name and new_name:
        return old_name == new_name
    # If the identity cannot be extracted, changed descriptions may indicate
    # different venues. Never use this alone to assert that two sites differ.
    return (str(old.get('description') or '').casefold() ==
            str(new.get('description') or '').casefold() and
            str(old.get('area') or '').casefold() ==
            str(new.get('area') or '').casefold())


def reconcile_dependencies(baseline: dict, proposal: dict, *, day_number: int,
                           origins: list[int | None],
                           changed_originals: set[int],
                           explicit_updates: dict[int, set[str]] | None = None) -> dict:
    """Remove unsupported derived claims without modifying unrelated stops.

    `origins` preserves original one-based positions after insert/removal. Only
    explicit removed identities and changes to the inbound predecessor are
    used. New metadata supplied in the patch is not taken as verified evidence.
    """
    result = deepcopy(proposal)
    explicit_updates = explicit_updates or {}
    prior = baseline['days'][day_number - 1]
    day = result['days'][day_number - 1]
    old_activities = prior['activities']
    current_activities = day['activities']
    active = set().union(*(_primary_aliases(a) for a in current_activities))
    removed = []
    for position, original in enumerate(old_activities, 1):
        if position not in changed_originals:
            continue
        removed.extend(sorted(_former_venue_aliases(original) - active))
    removed = list(dict.fromkeys(removed))

    # Inbound movement describes a relation with the preceding stop, rather
    # than an intrinsic property of the current attraction.
    for index, (item, origin) in enumerate(zip(current_activities, origins)):
        if origin is None:
            continue
        old_previous = origin - 1 if origin > 1 else None
        new_previous_origin = origins[index - 1] if index else None
        prior_changed = (new_previous_origin != old_previous or
                         new_previous_origin in changed_originals)
        if prior_changed:
            hint = str(item.get('transport_hint') or '')
            if hint and 'transport_hint' not in explicit_updates.get(origin, set()):
                item.pop('transport_hint', None)
            # Numeric movement estimates made for a different predecessor
            # are not valid, unless the model edits this activity explicitly.
            if 'transfer_minutes' not in explicit_updates.get(origin, set()):
                item.pop('transfer_minutes', None)
            if 'buffer_minutes' not in explicit_updates.get(origin, set()):
                item.pop('buffer_minutes', None)
            if _PREVIOUS_REFERENCE.search(str(item.get('why_this_fits') or '')):
                item.pop('why_this_fits', None)
        # A removed venue is not a current inbound travel origin.
        if any(_mentions(str(item.get('transport_hint') or ''), name) for name in removed):
            item.pop('transport_hint', None)

    def outdated(text: str) -> bool:
        return any(_mentions(text, name) for name in removed)

    for field in ('logistics', 'weather_alternative', 'budget_note', 'notes',
                  'intercity_transfer'):
        if outdated(str(day.get(field) or '')):
            day.pop(field, None)
    if outdated(str(day.get('title') or '')):
        day['title'] = f'Day {day_number}: Revised itinerary'
    if outdated(str(result.get('title') or '')):
        result['title'] = f"{result['destination']} itinerary"
    for field in ('summary', 'budget_strategy', 'arrival_departure_plan'):
        if outdated(str(result.get(field) or '')):
            result.pop(field, None)
    for field in ('planning_assumptions', 'key_tradeoffs', 'predeparture_checks'):
        if field in result:
            kept = [line for line in result[field] if not outdated(str(line))]
            if kept:
                result[field] = kept
            else:
                result.pop(field)

    # A changed first or final stop can invalidate unnamed departure claims
    # even when venue extraction was not possible.
    boundary_changed = (bool(changed_originals & {1, len(old_activities)}) or
                        origins[0] != 1 or origins[-1] != len(old_activities))
    if boundary_changed and _DEPARTURE_COPY.search(str(result.get('arrival_departure_plan') or '')):
        result.pop('arrival_departure_plan', None)

    return clear_inherited_stale_references(result, baseline)


def cross_area_zero_gap_issues(day: dict) -> list[tuple[int, str]]:
    """Reject provably impossible instantaneous transfers across named areas.

    Only different explicit administrative areas qualify. Adjacent sites in
    the same venue, unclear areas, and uncertain real-world ETA are not guessed.
    """
    from core.pipeline_integrity import _timed_activity

    issues = []
    activities = day.get('activities', [])
    for index in range(1, len(activities)):
        previous, current = activities[index - 1], activities[index]
        before = _timed_activity(previous)
        after = _timed_activity(current)
        if not before or not after or before[1] != after[0]:
            continue
        old_area = str(previous.get('area') or '').strip()
        new_area = str(current.get('area') or '').strip()
        explicit_travel = _TRAVEL_LEG.search(str(current.get('transport_hint') or ''))
        if (old_area and new_area and old_area.casefold() != new_area.casefold()
                and ((_ADMIN_AREA.search(old_area) and _ADMIN_AREA.search(new_area))
                     or explicit_travel)):
            issues.append((index + 1, 'A transfer between distinct places has no time allowance.'))
    return issues


def uncertain_area_transfers(document: dict) -> list[dict]:
    """Advisory: a feasible gap is not evidence of actual transfer duration."""
    from core.pipeline_integrity import _timed_activity
    notes = []
    for day in document.get('days', []):
        activities = day.get('activities', [])
        for index in range(1, len(activities)):
            previous, current = activities[index - 1], activities[index]
            first, second = _timed_activity(previous), _timed_activity(current)
            old, new = str(previous.get('area') or '').strip(), str(current.get('area') or '').strip()
            if not (first and second and old and new and old.casefold() != new.casefold()
                    and ((_ADMIN_AREA.search(old) and _ADMIN_AREA.search(new))
                         or _TRAVEL_LEG.search(str(current.get('transport_hint') or '')))):
                continue
            if not (isinstance(current.get('transfer_minutes'), int) and
                    not isinstance(current.get('transfer_minutes'), bool) and
                    current['transfer_minutes'] > 0):
                notes.append({'day': day['day'], 'activity_index': index + 1,
                              'from_area': old[:120], 'to_area': new[:120],
                              'available_minutes': second[0] - first[1],
                              'status': 'transfer_duration_unverified'})
    return notes[:100]


def stale_revision_references(document: dict, previous: dict) -> list[tuple[int, int | None, str]]:
    """Detect old explicit venue names still in routing/summary after revision.

    Activity descriptions may legitimately refer to a removed venue as a view,
    historical comparison or optional suggestion. Only fields that present
    the old venue as part of the active plan's logistics are hard checked.
    """
    if not previous:
        return []
    existing = {day['day']: day for day in previous.get('days', [])}
    active_names = set().union(*(_primary_aliases(item)
                                 for day in document.get('days', [])
                                 for item in day.get('activities', [])))
    issues = []
    for day in document.get('days', []):
        prior = existing.get(day.get('day'))
        if not prior or day == prior:
            continue
        current_records = day.get('activities', [])
        for old in prior.get('activities', []):
            # Inserting a new stop must not turn an unchanged legacy activity
            # into a fictional removed venue merely because its identity
            # cannot be inferred from a natural-language description.
            if any((item.get('description') == old.get('description') and
                    item.get('place_name') == old.get('place_name'))
                   for item in current_records):
                continue
            former_names = _former_venue_aliases(old) - active_names
            for former_name in former_names:
                for field in ('logistics', 'budget_note', 'notes', 'intercity_transfer'):
                    if _mentions(str(day.get(field) or ''), former_name):
                        issues.append((day['day'], None, field))
                for index, item in enumerate(day.get('activities', []), 1):
                    if _mentions(str(item.get('transport_hint') or ''), former_name):
                        issues.append((day['day'], index, 'transport_hint'))
                for field in ('summary', 'budget_strategy', 'arrival_departure_plan'):
                    if _mentions(str(document.get(field) or ''), former_name):
                        issues.append((day['day'], None, field))
    return list(dict.fromkeys(issues))



def clear_inherited_stale_references(document: dict, previous: dict) -> dict:
    """Discard stale *inherited* derived copy, regardless of revision route.

    The authoritative pre-commit check still rejects newly authored stale copy.
    This repairs only text inherited verbatim from a previous version; it
    cannot affect scheduling, user-supplied activity descriptions or evidence.
    """
    result = deepcopy(document)
    for issue_day, activity_index, field in stale_revision_references(result, previous):
        if field in ('summary', 'budget_strategy', 'arrival_departure_plan'):
            if result.get(field) == previous.get(field):
                result.pop(field, None)
        elif field in ('logistics', 'budget_note', 'notes', 'intercity_transfer'):
            affected = result['days'][issue_day - 1]
            baseline_day = previous['days'][issue_day - 1]
            if affected.get(field) == baseline_day.get(field):
                affected.pop(field, None)
        elif field == 'transport_hint' and activity_index is not None:
            affected = result['days'][issue_day - 1]['activities'][activity_index - 1]
            previous_hints = {str(row.get('transport_hint') or '')
                              for row in previous['days'][issue_day - 1]['activities']}
            if str(affected.get('transport_hint') or '') in previous_hints:
                affected.pop('transport_hint', None)
    return result
