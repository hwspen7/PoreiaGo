"""Durable traveler requirements for accepted itinerary revisions.

Only explicit, machine-observable traveler requirements are persisted. A draft
may stage requests, but only an accepted itinerary commits their constraints.
Normal edits must retain earlier constraints; a specific explicit override can
replace or remove one only when the replacement is observable in the candidate.
"""
from __future__ import annotations

from collections import Counter
from typing import Iterable

from .time_contract import activity_start_minute


def _key_text(value: object) -> str:
    return ' '.join(str(value or '').casefold().split())


def _visits(document: dict):
    if not isinstance(document, dict):
        return []
    return [(day.get('day'), item) for day in document.get('days', [])
            if isinstance(day, dict) for item in day.get('activities', [])
            if isinstance(item, dict)]


def _matching_place(item: dict, place: str) -> bool:
    name = _key_text(place)
    if len(name) < 3:
        return False
    actual = _key_text(item.get('place_name'))
    if actual:
        return name == actual or (len(name) >= 7 and name in actual)
    return name in _key_text(item.get('description'))


def _matching_topic(item: dict, topic: str) -> bool:
    from .validation_gate import _topic_matches
    return _topic_matches(item, topic)


def _within(item: dict, minute: int, tolerance: int = 0) -> bool:
    start = activity_start_minute(item)
    return start is not None and abs(start - minute) <= tolerance


def _identity(item: dict) -> tuple[str, str]:
    place = _key_text(item.get('place_name'))
    return ('place', place) if place else ('description', _key_text(item.get('description')))


def _after_matches(record: dict, day: int, item: dict) -> bool:
    start = activity_start_minute(item)
    if start is None or start <= record['minute']:
        return False
    if record.get('day') is not None and day != record['day']:
        return False
    identity = record.get('identity')
    if identity:
        return list(_identity(item)) == identity
    return True  # Compatibility with earlier generic after-time records.


def _target_days(intent: dict) -> set[int]:
    days = intent.get('target_days')
    return {day for day in days if type(day) is int and day > 0} if isinstance(days, list) else set()


def _day_allowed(day: int, intent: dict) -> bool:
    target = _target_days(intent)
    return not target or day in target


def _tolerance(value: object, default: int) -> int:
    return value if type(value) is int and 0 <= value <= 360 else default


def requirements_from_intent(intent: dict, document: dict,
                             previous: dict | None = None) -> list[dict]:
    """Derive only requirements fulfilled by the candidate, not LLM promises.

    The after-time requirement binds to an actual activity. For a revision,
    only a genuinely added activity qualifies as a new after-time commitment.
    """
    if not isinstance(intent, dict):
        return []
    visits = _visits(document)
    records = []
    place = intent.get('anchor_place')
    minute = intent.get('anchor_minute')
    if isinstance(place, str) and type(minute) is int and 0 <= minute < 1440:
        tolerance = _tolerance(intent.get('anchor_tolerance_minutes'), 0)
        found = next(((day, item) for day, item in visits
                      if _day_allowed(day, intent) and _matching_place(item, place)
                      and _within(item, minute, tolerance)), None)
        if found:
            rec = {'kind': 'anchor', 'key': 'anchor:' + _key_text(place),
                   'place': place, 'minute': minute, 'tolerance': tolerance}
            if _target_days(intent):
                rec['day'] = found[0]
            records.append(rec)
    topic, minute = intent.get('early_topic'), intent.get('early_target_minute')
    if isinstance(topic, str) and type(minute) is int and 0 <= minute < 1440:
        tolerance = _tolerance(intent.get('early_tolerance_minutes'), 60)
        found = next(((day, item) for day, item in visits
                      if _day_allowed(day, intent) and _matching_topic(item, topic)
                      and _within(item, minute, tolerance)), None)
        if found:
            rec = {'kind': 'early_topic', 'key': 'early:' + _key_text(topic),
                   'topic': topic, 'minute': minute, 'tolerance': tolerance}
            if _target_days(intent):
                rec['day'] = found[0]
            records.append(rec)
    minute = intent.get('not_before_minute')
    if type(minute) is int and 0 <= minute < 1440:
        existing = Counter(_identity(item) for _, item in _visits(previous or {}))
        added = []
        for day, item in visits:
            identity = _identity(item)
            if previous is not None and existing[identity]:
                existing[identity] -= 1
                continue
            if _day_allowed(day, intent) and activity_start_minute(item) is not None \
                    and activity_start_minute(item) > minute:
                added.append((day, item))
        if added:
            day, activity = added[-1]
            records.append({'kind': 'after', 'key': f'after:{minute}',
                            'minute': minute, 'day': day,
                            'identity': list(_identity(activity))})
    return records


def _same_place(a: object, b: object) -> bool:
    left, right = _key_text(a), _key_text(b)
    return len(left) >= 3 and len(right) >= 3 and (
        left == right or left in right or right in left)


def _removes_record(record: dict, intent: dict, document: dict) -> bool:
    name = intent.get('remove_place')
    if not name:
        return False
    if record.get('kind') == 'anchor' and _same_place(record.get('place'), name):
        return not any(_matching_place(activity, record.get('place'))
                       for _, activity in _visits(document))
    if record.get('kind') == 'after' and record.get('identity', [None])[0] == 'place':
        place = record['identity'][1]
        return _same_place(place, name) and not any(
            _matching_place(activity, place) for _, activity in _visits(document))
    return False


def _replaces_record(record: dict, intent: dict, document: dict) -> bool:
    """Explicit replacement is allowed only when its new target is fulfilled."""
    if not isinstance(intent, dict):
        return False
    kind = record.get('kind')
    if kind == 'anchor' and _key_text(intent.get('anchor_place')) == _key_text(record.get('place')):
        return any(row['key'] == record.get('key')
                   for row in requirements_from_intent(intent, document))
    if kind == 'early_topic' and _key_text(intent.get('early_topic')) == _key_text(record.get('topic')):
        return any(row['key'] == record.get('key')
                   for row in requirements_from_intent(intent, document))
    return False


def unresolved_requirements(document: dict, requirements: list[dict] | None,
                            intent: dict | None = None,
                            superseding_intents: Iterable[dict] | None = None) -> list[dict]:
    """Fail closed for supported retained constraints and exact changed visits."""
    issues = []
    visits = _visits(document)
    intents = [value for value in (superseding_intents or []) if isinstance(value, dict)]
    if isinstance(intent, dict):
        intents.append(intent)
    for record in requirements or []:
        if not isinstance(record, dict):
            continue
        minute, kind = record.get('minute'), record.get('kind')
        if kind not in {'anchor', 'early_topic', 'after'}:
            continue  # Future versions own other kinds, never reinterpret them.
        if type(minute) is not int or not 0 <= minute < 1440:
            issues.append({'code': 'INVALID_STORED_JOURNEY_CONSTRAINT',
                           'detail': 'An existing saved traveler constraint has invalid timing.'})
            continue
        if any(_removes_record(record, new, document) or _replaces_record(record, new, document)
               for new in intents):
            continue
        if kind == 'anchor':
            valid = any((record.get('day') is None or day == record['day'])
                        and _matching_place(item, record.get('place', ''))
                        and _within(item, minute, _tolerance(record.get('tolerance'), 0))
                        for day, item in visits)
        elif kind == 'early_topic':
            valid = any((record.get('day') is None or day == record['day'])
                        and _matching_topic(item, record.get('topic', ''))
                        and _within(item, minute, _tolerance(record.get('tolerance'), 60))
                        for day, item in visits)
        else:
            valid = any(_after_matches(record, day, item) for day, item in visits)
        if not valid:
            label = str(record.get('place') or record.get('topic') or 'late activity')
            issues.append({'code': 'PRIOR_TRAVELER_CONSTRAINT_LOST',
                           'detail': f'An earlier traveler request for {label} near/after '
                                     f'{minute // 60:02d}:{minute % 60:02d} was lost. '
                                     'Keep the request or explicitly change it.'})
    return issues


def reconcile_requirements(existing: list[dict], intents: Iterable[dict],
                           document: dict, previous: dict | None = None) -> list[dict]:
    """Compute the post-commit constraint set without modifying the database.

    Call only after the shared validation gate accepts the candidate, then
    atomically write this result alongside the itinerary version.
    """
    current = {record['key']: dict(record) for record in existing
               if isinstance(record, dict) and isinstance(record.get('key'), str)}
    for intent in intents:
        if not isinstance(intent, dict):
            continue
        for key, record in list(current.items()):
            if _removes_record(record, intent, document):
                del current[key]
        for record in requirements_from_intent(intent, document, previous):
            current[record['key']] = record
    return [current[key] for key in sorted(current)]


def compact_intents(intents: Iterable[dict]) -> list[dict]:
    """Keep independent pending requests but supersede explicit same-target edits.

    This is used only for editable drafts; verified constraint replacement still
    requires a successful pre-commit check against the actual activities.
    """
    active = []
    for intent in intents:
        if not isinstance(intent, dict):
            continue
        anchor = _key_text(intent.get('anchor_place'))
        topic = _key_text(intent.get('early_topic'))
        removal = intent.get('remove_place')
        if anchor:
            active = [old for old in active
                      if _key_text(old.get('anchor_place')) != anchor]
        if topic:
            active = [old for old in active
                      if _key_text(old.get('early_topic')) != topic]
        if removal:
            active = [old for old in active
                      if not _same_place(old.get('anchor_place'), removal)]
        active.append(intent)
    return active[-20:]
