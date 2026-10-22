"""Deterministic, read-only quality signals for travel-planning evaluations.

These signals are diagnostic proxies, not proof of travel feasibility or parity
with another language model. A human must review the recorded itineraries.
"""
from __future__ import annotations

import re
from typing import Any
from .content_audit import audit_itinerary


def itinerary_text(itinerary: dict) -> str:
    """Flatten only the rendered trip content, without untrusted model traces."""
    parts = [str(itinerary.get('destination', '')), str(itinerary.get('title', '')),
             str(itinerary.get('summary', ''))]
    for day in itinerary.get('days', []):
        parts.append(str(day.get('title', '')))
        parts.append(str(day.get('notes', '')))
        parts.extend(str(activity.get('description', ''))
                     for activity in day.get('activities', []) if isinstance(activity, dict))
    return '\n'.join(parts)


def _matches_any(content: str, synonyms: list[str]) -> bool:
    return any(re.search(r'(?<!\w)' + re.escape(term) + r'(?!\w)', content, re.I)
               for term in synonyms if isinstance(term, str) and term)


def assess_itinerary(case: dict, itinerary: dict, *, previous: dict | None = None,
                     delta: dict | None = None, evidence: list[dict] | None = None) -> dict:
    """Score observable itinerary properties; return evidence for manual review."""
    from .trip_state import TripValidationError, validate_itinerary

    operation = case['kind']
    expected_days = int(case.get('days') or (len(previous['days']) if previous else 0))
    try:
        validated = validate_itinerary(itinerary, previous=previous,
                                       operation=operation,
                                       destination_hint=case.get('destination'))
    except TripValidationError as exc:
        return {'score': 0, 'passed_structure': False, 'task_success': False,
                'checks': {'schema': False}, 'warnings': [str(exc)]}
    days = validated['days']
    checks: dict[str, Any] = {'schema': True, 'day_count': len(days) == expected_days}
    warnings: list[str] = []
    if not checks['day_count']:
        warnings.append(f'Expected {expected_days} days; received {len(days)}.')

    if operation == 'revise':
        target = case.get('changed_day')
        actual_changes = [item['day'] for item in days
                          if previous and item != previous['days'][item['day'] - 1]]
        checks['target_day_changed'] = actual_changes == [target]
        checks['other_days_unchanged'] = bool(previous) and all(
            days[i] == previous['days'][i]
            for i in range(len(days)) if i != target - 1)
        checks['destination_preserved'] = bool(previous) and (
            validated['destination'].casefold() == previous['destination'].casefold())
        checks['preferences_preserved'] = bool(previous) and all(
            validated['preferences'].get(key) == value
            for key, value in previous.get('preferences', {}).items())
        if not checks['target_day_changed']:
            warnings.append('The revision did not exclusively change the requested day.')
        if not checks['preferences_preserved']:
            warnings.append('Existing traveler preferences were changed or lost.')
        weights = {'schema': 10, 'day_count': 15, 'target_day_changed': 30,
                   'other_days_unchanged': 25, 'destination_preserved': 10,
                   'preferences_preserved': 10}
    else:
        act_counts = [len(day['activities']) for day in days]
        checks['two_activities_per_day'] = all(n >= 2 for n in act_counts)
        descriptions = [a['description'] for day in days for a in day['activities']]
        checks['specific_activities'] = (bool(descriptions) and
            sum(len(d.strip()) >= 24 for d in descriptions) / len(descriptions) >= 0.75)
        raw = itinerary_text(validated)
        required = case.get('required_terms', [])
        checks['named_places_mentioned'] = all(
            _matches_any(raw, [term]) for term in required)
        signals = case.get('constraint_signals', [])
        checks['constraint_signals'] = all(_matches_any(raw, group) for group in signals)
        # Long descriptions can still be repetitive or wholly generic.
        normalized = [re.sub(r'\b(?:day|d[ií]a)\s*\d+\b|\b\d+\b', '',
                             re.sub(r'[^\w\s]', ' ', description).casefold()).strip()
                      for description in descriptions]
        checks['distinct_activities'] = (len(set(normalized)) / max(1, len(normalized))) >= 0.85
        checks['practical_notes'] = sum(
            len(str(day.get('notes', '')).strip()) >= 32 for day in days
        ) >= max(1, (len(days) * 3 + 4) // 5)
        # An unchecked assertion of step-free access is especially misleading
        # when the request concerns a traveler with limited mobility.
        mobility_request = bool(re.search(
            r'\b(?:accessible|accessibility|mobility|wheelchair|step[- ]free)\b',
            case.get('request', ''), re.I))
        checks['no_unsupported_accessibility'] = not (mobility_request and re.search(
            r'\b(?:fully accessible|no walking required|guaranteed step[- ]free|'
            r'all paths accessible|completely wheelchair[- ]accessible)\b',
            raw, re.I))
        if not checks['distinct_activities']:
            warnings.append('Activities are repeated or insufficiently differentiated across days.')
        if not checks['practical_notes']:
            warnings.append('Most days lack useful route, pacing or practical notes.')
        if not checks['no_unsupported_accessibility']:
            warnings.append('Accessibility claim needs verified venue or transit evidence.')
        # Do not treat exact prices/timetables as verified simply because they
        # appear in the model output. Review flagged statements manually.
        checks['no_unverified_confirmations'] = not bool(re.search(
            r'\b(?:confirmed booking|guaranteed availability|verified opening hours|'
            r'confirmed reservation|live ticket availability)\b', raw, re.I))
        if not checks['two_activities_per_day']:
            warnings.append('Some days contain fewer than two activities.')
        if not checks['specific_activities']:
            warnings.append('Many activity descriptions are generic or very short.')
        if not checks['named_places_mentioned']:
            warnings.append('One or more requested destination names are absent.')
        if not checks['constraint_signals']:
            warnings.append('Requested interests or constraints need human review.')
        if not checks['no_unverified_confirmations']:
            warnings.append('Possible unsupported real-time confirmation; verify manually.')
        weights = {'schema': 15, 'day_count': 15, 'two_activities_per_day': 10,
                   'specific_activities': 10, 'named_places_mentioned': 10,
                   'constraint_signals': 15, 'no_unverified_confirmations': 10,
                   'distinct_activities': 5, 'practical_notes': 5,
                   'no_unsupported_accessibility': 5}
    score = sum(weight for name, weight in weights.items() if checks.get(name))
    # Schema success and task completion are distinct. Hard constraints must not
    # be reported as a successful trip merely because its JSON is well formed.
    hard = ('schema', 'day_count', 'target_day_changed', 'other_days_unchanged',
            'destination_preserved', 'preferences_preserved') if operation == 'revise' else (
            'schema', 'day_count', 'named_places_mentioned', 'constraint_signals',
            'no_unverified_confirmations', 'no_unsupported_accessibility')
    success = all(checks.get(name, False) for name in hard)
    review = audit_itinerary(validated, request=case.get('request', ''),
                             expected_days=expected_days, evidence=evidence)
    source_matches = (review.get('traveler_review') or {}).get('place_evidence') or {}
    route_evidence = (review.get('traveler_review') or {}).get('route_evidence') or {}
    return {'score': score,
            'benchmark_evidence': {
                'candidate_count': source_matches.get('candidate_count', 0),
                'linked_activities': len(source_matches.get('matches') or []),
                'unmatched_named_activities': len(source_matches.get('unmatched') or []),
                'mapped_walking_legs_checked': route_evidence.get('assessed_walking_legs', 0),
                'walking_lower_bound_warnings': len(route_evidence.get('warnings') or []),
                'fact_verification': 'not_performed'},
            'content_quality_score': review['score'],
            'content_quality_dimensions': review['dimensions'],
            'content_quality_findings': review['findings'],
            'traveler_quality_review': review.get('traveler_review', {}),
            'verification_status': review.get('verification_status', 'not_assessed'),
            'human_review_required': review['human_review_required'],
            'passed_structure': True, 'task_success': success,
            'checks': checks, 'warnings': warnings, 'delta': delta,
            'content_quality_warnings': [warning for warning in warnings
                                         if 'Activity' in warning or 'Activities' in warning
                                         or 'notes' in warning or 'descriptions' in warning]}



def assess_discussion(case: dict, frame: Any, answer: str | None,
                      *, previous: dict | None = None,
                      revision_delta: dict | None = None) -> dict:
    """Require an actual answer, grounded reference, and no context denial."""
    operation = frame.task.get('operation') if frame is not None else None
    answer = answer or ''
    denied = bool(re.search(
        r"(?:no tengo acceso|no dispongo de|no puedo acceder|no conozco|"
        r"no (?:previous|prior|saved|existing) (?:itinerary|trip|plan)|"
        r"(?:do not|don't|cannot|can't) have access|"
        r"(?:please|por favor) (?:provide|share|proporciona|comparte))",
        answer, re.I))
    checks = {
        'discussion_operation': operation == 'discuss',
        'no_trip_mutation': operation not in {'create', 'revise'},
        'substantive_answer': len(answer.strip()) >= 120,
        'context_available': not denied,
    }
    if case.get('language') == 'es':
        checks['language_signal'] = bool(re.search(
            r'\b(?:el|la|los|las|para|día|días|viaje|visitar|itinerario|cambio|cambios)\b',
            answer, re.I))
    else:
        checks['language_signal'] = len(answer.strip()) > 0
    if previous:
        checks['context_reference'] = bool(re.search(
            r'\b(?:day|days|día|días|third|tercer|tercero|itinerary|itinerario|'
            r'changes|cambios)\b', answer, re.I))
    else:
        checks['context_reference'] = True
    if revision_delta and revision_delta.get('changed_days'):
        # The evaluation checks changed-day awareness, not a made-up state claim.
        changes = revision_delta['changed_days']
        checks['changed_day_reference'] = any(re.search(
            rf'(?<!\d){day}(?!\d)|\b(?:third|tercer|tercero)\b' if day == 3
            else rf'(?<!\d){day}(?!\d)', answer, re.I) for day in changes)
    else:
        checks['changed_day_reference'] = True
    weights = {'discussion_operation': 15, 'no_trip_mutation': 15,
               'substantive_answer': 20, 'context_available': 20,
               'language_signal': 10, 'context_reference': 10,
               'changed_day_reference': 10}
    success = all(checks.values())
    return {'score': sum(v for k, v in weights.items() if checks[k]) if success else 0,
            'passed_structure': success, 'task_success': success,
            'checks': checks,
            'warnings': [f'Failed discussion check: {k}' for k, v in checks.items() if not v]}


def compare_scores(rows: list[dict]) -> list[dict]:
    """Pair matching cases; never treat automatic score as proof of superiority."""
    grouped: dict[str, dict[str, list[dict]]] = {}
    for row in rows:
        if isinstance(row.get('score'), int):
            grouped.setdefault(row['case_id'], {}).setdefault(row['model'], []).append(row)
    return [{'case_id': case_id,
             'model_scores': {model: round(sum(r['score'] for r in trials) / len(trials), 2)
                              for model, trials in models.items()},
             'success_rates': {model: round(sum(bool(r.get('task_success',
                                                       r.get('passed_structure', False)))
                                              for r in trials) / len(trials), 3)
                               for model, trials in models.items()},
             'attempts': {model: len(trials) for model, trials in models.items()}}
            for case_id, models in grouped.items()]
