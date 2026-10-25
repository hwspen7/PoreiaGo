"""Read-only, deterministic travel-plan quality signals.

Scores are completeness proxies, never claims of real-world accuracy. The
same audit works on user plans from any location or language, without a live
provider call or an LLM judge. No Skill registry is touched.
"""
from __future__ import annotations

import re
from quality.traveler_review import review_traveler_quality
from core.pipeline_integrity import schedule_invariants
from typing import Any


def _filled(value: Any, minimum: int = 18) -> bool:
    return isinstance(value, str) and len(value.strip()) >= minimum


def _fraction(values: list[bool]) -> float:
    return sum(values) / len(values) if values else 0.0


def _time_minutes(value: Any) -> int | None:
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r'\s*([01]?\d|2[0-3]):([0-5]\d)\s*', value)
    return int(match.group(1)) * 60 + int(match.group(2)) if match else None


def audit_itinerary(itinerary: dict, *, request: str = '',
                    expected_days: int | None = None,
                    evidence: list[dict] | None = None) -> dict:
    """Return actionable quality findings without rewriting the itinerary.

    Only observable attributes are assessed. Geocoding, operating hours,
    prices and physical access cannot be verified from prose alone.
    """
    days = itinerary.get('days', []) if isinstance(itinerary, dict) else []
    if not isinstance(days, list) or not days:
        return {'score': 0, 'dimensions': {}, 'findings': ['Missing day-by-day plan.'],
                'human_review_required': True}
    findings: list[str] = []
    activities = [a for day in days if isinstance(day, dict)
                  for a in day.get('activities', []) if isinstance(a, dict)]
    descriptions = [str(a.get('description', '')).strip() for a in activities]
    normalized = [re.sub(r'\W+', ' ', description.casefold()).strip()
                  for description in descriptions]
    unique = len(set(normalized)) / max(len(normalized), 1)
    specifics = _fraction([len(text) >= 55 for text in descriptions])
    generic_experiences = _fraction([
        bool(re.search(r'^(?:(?:morning|afternoon|evening)[: ]+)?'
                       r'(?:visit|explore|see|enjoy|discover|go to|have)\s+'
                       r'(?:(?:a|the|some)\s+)?'
                       r'(?:local|nearby|some|city|downtown)\s+'
                       r'(?:museum|attraction|restaurant|dinner|lunch|market|'
                       r'shops?|gallery|area|neighborhood|food|sights?)\.?$',
                       text.strip(), re.I))
        for text in descriptions])
    non_generic_experiences = round(100 * (1.0 - generic_experiences))
    # Full prose alone does not identify a navigable place.  A clear proper
    # venue name or useful neighborhood is necessary for maximal specificity.
    identifiable = _fraction([
        bool(a.get('place_name')) or bool(re.search(
            r'\b(?:[A-Z][A-Za-z-]+\s+){1,5}'
            r'(?:Museum|Conservatory|Gallery|Market|Park|Garden|Temple|'
            r'Shrine|University|Village|District|Center|Centre)\b',
            str(a.get('description', '')))) or
        bool(re.search(r'\b(?:German Village|Short North|Downtown|University District)\b',
                       str(a.get('area') or '')))
        for a in activities])
    purpose = _fraction([_filled(a.get('why_this_fits'), 24) for a in activities])
    timed = _fraction([_time_minutes(a.get('start_time') or a.get('time')) is not None or
                       str(a.get('time', '')).lower() in {'morning', 'afternoon', 'evening'}
                       for a in activities])
    cluster = _fraction([_filled(day.get('area_cluster'), 3)
                         for day in days])
    logistics = _fraction([_filled(day.get('logistics'), 24)
                           or _filled(day.get('notes'), 45) for day in days])
    contingency = _fraction([_filled(day.get('weather_alternative'), 24)
                             for day in days])
    detailed_contingency = _fraction([
        _filled(day.get('weather_alternative'), 55)
        for day in days])
    daily_load = [len(day.get('activities', [])) for day in days]
    reasonable = _fraction([1 <= n <= 5 for n in daily_load])
    # Explicit contradictions reduce the timing dimension itself, not only a
    # global warning penalty. A 100/100 schedule with overlaps is misleading.
    incompatible_days = sum(bool(schedule_invariants(day)) for day in days)
    timing_penalty = round(100 * incompatible_days / max(len(days), 1))
    full_day_density = _fraction([
        n >= 3 or (index in (0, len(days) - 1) and n >= 2)
        for index, n in enumerate(daily_load)])
    meal_interest = bool(re.search(r'\b(?:food|meals?|dining|cuisine|culinary|restaurant|gastronom)\w*\b',
                                   request, re.I))
    meal_coverage = _fraction([
        any(re.search(r'\b(?:breakfast|lunch|dinner|food|cuisine|dining|restaurant|'
                      r'market|eat|tasting|snack|dish|meal)\b',
                      str(activity.get('description', '')), re.I)
            for activity in day.get('activities', []))
        for day in days])
    for i, day in enumerate(days, 1):
        if len(day.get('activities', [])) > 5:
            findings.append(f'Day {i}: too many stops; prioritize essential experiences.')
        ordered = []
        for activity in day.get('activities', []):
            start = _time_minutes(activity.get('start_time') or activity.get('time'))
            if start is None:
                continue
            if ordered and start < ordered[-1]:
                findings.append(f'Day {i}: time order is inconsistent.')
                break
            ordered.append(start)
        # Only enforce durations when both exact start times and a duration are stated.
        for left, right in zip(day.get('activities', []), day.get('activities', [])[1:]):
            start, following = (_time_minutes(left.get('start_time') or left.get('time')),
                                _time_minutes(right.get('start_time') or right.get('time')))
            duration = left.get('duration_minutes')
            if (start is not None and following is not None
                    and isinstance(duration, int) and not isinstance(duration, bool)
                    and start + duration > following):
                findings.append(f'Day {i}: activity times overlap; account for dwell time and transfers.')
                break
    interest = ('interest' in request.casefold() or 'prefer' in request.casefold()
                or 'young' in request.casefold() or 'food' in request.casefold())
    budget_request = bool(re.search(r'\b(?:budget|under|cost|spending|eur|usd|gbp)\b|[€$£]', request, re.I))
    mobility = bool(re.search(r'\b(?:mobility|wheelchair|accessible|step[- ]free)\b', request, re.I))
    overview = _filled(itinerary.get('arrival_departure_plan'), 45)
    assumptions = isinstance(itinerary.get('planning_assumptions'), list) and bool(itinerary['planning_assumptions'])
    tradeoffs = isinstance(itinerary.get('key_tradeoffs'), list) and bool(itinerary['key_tradeoffs'])
    checked = isinstance(itinerary.get('predeparture_checks'), list) and bool(itinerary['predeparture_checks'])
    budget = _filled(itinerary.get('budget_strategy'), 45)
    verification = _fraction([_filled(a.get('verification_needed'), 18)
                              for a in activities])
    boilerplate_checks = _fraction([
        bool(re.fullmatch(r'(?:check|confirm|verify)\s+(?:the\s+)?'
                          r'(?:opening hours|availability|hours|details|'
                          r'prices|times|booking|reservations)[.! ]*',
                          str(a.get('verification_needed', '')).strip(), re.I))
        for a in activities])
    indicative_cost_coverage = _fraction([
        _filled(a.get('cost_estimate'), 5) and bool(re.search(
            r'\d', str(a.get('cost_estimate', ''))))
        for a in activities])
    coverage = (len(days) == expected_days if expected_days else True)
    # Ready-to-use traveler content differs from the mere presence of metadata.
    # These tests deliberately make no claim that a place or price is verified.
    dayparts = ('morning', 'afternoon', 'evening', 'breakfast', 'lunch', 'dinner')
    actionable_days = _fraction([
        any(any(token in str(a.get('time', '')).casefold() for token in dayparts)
            or _time_minutes(a.get('start_time') or a.get('time')) is not None for a in day.get('activities', []))
        and len(day.get('activities', [])) >= (2 if i in (0, len(days)-1) else 3)
        for i, day in enumerate(days)])
    actionable_backups = _fraction([
        _filled(day.get('weather_alternative'), 40) and
        not re.search(r'^(?:visit (?:a |an )?museum|find (?:an )?indoor activity|'
                      r'choose an indoor option|check the weather)[.! ]*$',
                      str(day.get('weather_alternative', '')).strip(), re.I)
        for day in days])
    specific_transport = _fraction([
        any(word in str(day.get('logistics', '')).casefold()
            for word in ('walk', 'train', 'metro', 'subway', 'bus', 'taxi',
                         'tram', 'ferry', 'rail', 'car', 'ride', 'transit'))
        for day in days])
    practical_traveler_readiness = round(100 * (
        0.45 * actionable_days + 0.30 * actionable_backups +
        0.25 * specific_transport))
    traveler_review = review_traveler_quality(itinerary, evidence=evidence,
                                              request=request)
    audit_codes = [entry['code'] for entry in traveler_review['issues']]
    source_matching = traveler_review.get('place_evidence') or {}
    matched = len(source_matching.get('matches') or [])
    unmatched = len(source_matching.get('unmatched') or [])
    candidate_traceability = (round(100 * matched / (matched + unmatched))
                              if matched + unmatched else None)
    departure_penalty = (40 if 'departure_flight_unspecified' in audit_codes
                         and re.search(r'\b(?:flight|airport|airfare)\b', request, re.I) else 0)
    variety_penalty = min(60, 12 * sum(
        code in {'repeated_dish', 'repeated_weather_backup',
                 'repeated_weather_venue'} for code in audit_codes))
    dinner_penalty = min(35, round(100 * 0.35 * audit_codes.count('no_dinner_option') /
                                   max(1, len(days) - 2)))
    dimensions = {
        'complete_days': 100 if coverage else 0,
        'experience_specificity': round(100 * (0.3 * specifics +
                                               0.25 * purpose +
                                               0.25 * (1 - generic_experiences) +
                                               0.2 * identifiable)),
        'experience_actionability': non_generic_experiences,
        'candidate_traceability': candidate_traceability,
        # These are plan-evidence scores, not verified walking or transfer feasibility.
        'geographic_flow': round(80 * (0.5 * cluster + 0.5 * logistics)),
        'pacing_and_timing': max(0, round(100 * (0.4 * reasonable +
                                         0.35 * full_day_density + 0.25 * timed))
                                   - timing_penalty),
        'resilience': round(100 * (0.35 * contingency +
                                   0.25 * detailed_contingency + 0.4 * tradeoffs)),
        'departure_and_assumptions': max(0, round(100 * (
            0.7 * overview + 0.3 * assumptions)) - departure_penalty),
        'practical_readiness': round(100 * (0.4 * checked +
                                            0.4 * verification +
                                            0.2 * (1 - boilerplate_checks))),
        'personalization': round(100 * purpose) if interest else None,
        'budget_awareness': (round(100 * (0.35 * budget + 0.2 * tradeoffs +
                                        0.2 * indicative_cost_coverage +
                                        0.25 * _fraction([
                                            _filled(day.get('budget_note'), 35)
                                            for day in days])))
                             if budget_request else None),
        'meal_coverage': max(0, round(100 * meal_coverage) - dinner_penalty)
                         if meal_interest else None,
        'accessibility_caution': (100 if checked else 0) if mobility else None,
        'variety': max(0, round(100 * unique) - variety_penalty),
        'traveler_readiness': practical_traveler_readiness,
    }
    weights = {
        'candidate_traceability': 0,  # Incomplete provider samples are diagnostic only.
        'complete_days': 10, 'experience_specificity': 18,
        'experience_actionability': 8,
        'geographic_flow': 15, 'pacing_and_timing': 10,
        'resilience': 10, 'departure_and_assumptions': 10,
        'practical_readiness': 10, 'personalization': 6,
        'budget_awareness': 5, 'accessibility_caution': 4,
        'variety': 2, 'meal_coverage': 8, 'traveler_readiness': 12,
    }
    active = {name: value for name, value in dimensions.items() if value is not None}
    denominator = sum(weights[name] for name in active)
    score = round(sum(weights[name] * value for name, value in active.items()) / denominator)
    for name, label in (
        ('experience_specificity', 'Give activity-specific reasons and distinct local experiences.'),
        ('experience_actionability', 'Replace generic stops with identifiable experiences or named areas.'),
        ('geographic_flow', 'Explain neighborhood grouping and realistic transfers.'),
        ('resilience', 'Add conditional weather alternatives and meaningful trade-offs.'),
        ('departure_and_assumptions', 'Handle arrival/departure windows and state unknown assumptions.'),
        ('practical_readiness', 'Flag important booking and verification checks.'),
        ('personalization', 'Explain why the selected experiences fit the traveler.'),
        ('budget_awareness', 'Explain budget trade-offs without inventing live prices.'),
        ('meal_coverage', 'Include a relevant food or meal experience on more travel days.'),
        ('pacing_and_timing', 'Give a realistic daypart or start time and sufficient daily activities.'),
        ('traveler_readiness', 'Make each day usable: actionable schedule, transport and concrete alternatives.'),
    ):
        if dimensions[name] is not None and dimensions[name] < 65:
            findings.append(label)
    if candidate_traceability is not None and unmatched:
        findings.append(
            f'{unmatched} explicitly named activity venues are not in the bounded '
            'Skill candidate sample. Verify their identity and location before travel; '
            'absence from this sample does not establish nonexistence.')
    if generic_experiences > 0:
        findings.append('Generic activity placeholders require traveler-ready venue or area detail.')
    if boilerplate_checks > 0.3:
        findings.append('Repeated verification boilerplate should identify the specific decision or uncertainty.')
    if budget_request and indicative_cost_coverage < 0.35:
        findings.append('Budget request lacks enough labeled, indicative local-currency amounts; do not invent live prices.')
    if unique < 0.85:
        findings.append('Repeated activity descriptions suggest insufficient variety.')
    unsupported = re.search(
        r'\b(?:confirmed booking|guaranteed availability|verified opening hours|'
        r'confirmed reservation|no walking required|guaranteed step[- ]free)\b',
        '\n'.join(descriptions + [str(itinerary.get('summary', ''))]), re.I)
    if unsupported:
        findings.append('Unverified absolute claim requires external evidence.')
    if not any(_time_minutes(a.get('start_time') or a.get('time')) is not None for a in activities):
        findings.append('No exact activity start times were provided; validate daypart sequencing.')
    if cluster and logistics:
        findings.append('Area clusters and transfer feasibility still require geographic verification.')
    for issue in traveler_review['issues']:
        if issue['severity'] != 'info':
            findings.append(f"{issue['code']}: {issue['message']}")
    score = max(0, score - traveler_review['score_penalty'])
    return {'score': max(0, min(100, score)), 'dimensions': dimensions,
            'traveler_review': traveler_review,
            'verification_status': traveler_review['verification_status'],
            'findings': list(dict.fromkeys(findings)),
            'human_review_required': True,
            'scope': 'structural and content evidence only; not geographic or live-data verification'}
