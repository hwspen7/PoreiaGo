"""Comprehensive regression tests for structured and reliable travel content."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from quality.content_audit import audit_itinerary
from planning.trip_state import (TripValidationError, render_itinerary,
                              trip_delta, validate_itinerary)
from intelligence.model_agent import ITINERARY_REPAIR_SYSTEM
from evaluation.audit_travel_content import audit_file


def sample_trip():
    return {
        'destination': 'Example Region', 'title': 'A thoughtful regional escape',
        'summary': 'Three days of art, quiet neighborhoods and regional food.',
        'budget_strategy': 'Prioritize two signature experiences and choose modest dining elsewhere.',
        'arrival_departure_plan': 'Arrival and departure hours are not known. Keep both ends flexible.',
        'planning_assumptions': ['Dates and arrival times are not provided.'],
        'key_tradeoffs': ['Use a short regional transfer to reduce unnecessary backtracking.'],
        'predeparture_checks': ['Confirm attraction accessibility and advance-ticket rules.'],
        'preferences': {'pace': 'balanced', 'interests': 'art and regional food'},
        'days': [{
            'day': i, 'title': f'Local discovery day {i}',
            'theme': 'Art, food and neighborhood culture',
            'area_cluster': f'Neighborhood cluster {i}', 'pace': 'balanced',
            'logistics': 'Group nearby experiences before making one optional area transfer.',
            'weather_alternative': 'If weather is poor, substitute a local indoor exhibition.',
            'budget_note': 'The optional guided experience may cost more than self-guided sightseeing.',
            'notes': 'Leave time to rest between major stops and avoid unnecessary transfers.',
            'activities': [
                {'description': f'Explore an established arts area on day {i} and compare local exhibits.',
                 'time': '09:30', 'duration_minutes': 80, 'cost_level': 'moderate',
                 'area': 'Museum district', 'why_this_fits': 'Fits the traveler interest in art.',
                 'booking_advice': 'Check whether advance entry is needed.',
                 'verification_needed': 'Confirm opening days before visiting.'},
                {'description': f'Sample regional food at a local market on day {i} and learn about seasonal dishes.',
                 'time': '12:00', 'duration_minutes': 70, 'cost_level': 'low',
                 'why_this_fits': 'Matches the stated interest in regional food.',
                 'transport_hint': 'Allow transfer time between the museum and market.'},
            ],
        } for i in range(1, 4)],
    }


def test_preserves_rich_detail_after_validation_and_render():
    validated = validate_itinerary(sample_trip())
    assert validated['days'][0]['activities'][0]['duration_minutes'] == 80
    assert validated['days'][0]['weather_alternative'].startswith('If weather')
    message = render_itinerary(validated)
    for detail in ('Museum district', 'Allow about 80 minutes', 'Alternative:',
                   'Planning assumptions', 'Booking:', 'Arrival and departure',
                   'Cost level: moderate', 'Area:', 'Logistics:', 'Choices and trade-offs'):
        assert detail in message


def test_legacy_itinerary_remains_valid_without_new_fields():
    old = {'destination': 'Example Region', 'days': [{'day': 1, 'title': 'Day one',
           'activities': [{'description': 'Explore a neighborhood.'}]}]}
    validated = validate_itinerary(old)
    assert 'budget_strategy' not in validated
    assert validated['days'][0]['activities'][0] == old['days'][0]['activities'][0]


def test_targeted_revision_preserves_unrelated_day_and_rich_metadata():
    baseline = validate_itinerary(sample_trip())
    improved = deepcopy(baseline['days'][1])
    improved['activities'][0]['why_this_fits'] = 'A more interactive art experience suits the new preference.'
    revised = validate_itinerary({'changes': [improved]}, previous=baseline, operation='revise')
    assert revised['days'][0] == baseline['days'][0]
    assert revised['days'][2] == baseline['days'][2]
    assert revised['budget_strategy'] == baseline['budget_strategy']
    assert revised['arrival_departure_plan'] == baseline['arrival_departure_plan']
    assert trip_delta(baseline, revised)['changed_days'] == [2]


def test_top_level_revisions_are_tracked_not_silently_ignored():
    previous = validate_itinerary(sample_trip())
    revised = validate_itinerary({
        'days': deepcopy(previous['days']),
        'planning_assumptions': ['Departure time has now been confirmed.'],
    }, previous=previous, operation='revise')
    assert trip_delta(previous, revised)['changed_trip_fields'] == ['planning_assumptions']


@pytest.mark.parametrize('field,value', [
    ('duration_minutes', 0), ('duration_minutes', True),
    ('duration_minutes', 900), ('cost_level', 'guaranteed-free'),
    ('area', {'nested': 'no'}), ('booking_advice', ['unbounded']),
])
def test_rejects_bad_rich_activity_content(field, value):
    trip = sample_trip()
    trip['days'][0]['activities'][0][field] = value
    with pytest.raises(TripValidationError):
        validate_itinerary(trip)


@pytest.mark.parametrize('field,value', [
    ('pace', 'impossible'), ('area_cluster', ['two', 'areas']),
    ('logistics', ''), ('weather_alternative', {'if_rain': 'go inside'}),
])
def test_rejects_bad_rich_day_content(field, value):
    trip = sample_trip()
    trip['days'][1][field] = value
    with pytest.raises(TripValidationError):
        validate_itinerary(trip)


def test_rejects_unbounded_trip_assumptions():
    trip = sample_trip()
    trip['planning_assumptions'] = ['detail'] * 9
    with pytest.raises(TripValidationError):
        validate_itinerary(trip)


def test_audit_reports_specific_gap_for_generic_plan():
    rich = validate_itinerary(sample_trip())
    generic = deepcopy(rich)
    for day in generic['days']:
        day.pop('area_cluster')
        day.pop('weather_alternative')
        day.pop('logistics')
        for act in day['activities']:
            act.pop('why_this_fits', None)
            act['description'] = 'Explore local attractions.'
    rich_audit = audit_itinerary(rich, request='Plan an art trip with food', expected_days=3)
    weak_audit = audit_itinerary(generic, request='Plan an art trip with food', expected_days=3)
    assert rich_audit['score'] > weak_audit['score']
    assert rich_audit['human_review_required'] is True
    assert any('geographic' in item or 'neighborhood' in item for item in weak_audit['findings'])


def test_audit_detects_time_conflict_and_overloading():
    trip = sample_trip()
    trip['days'][0]['activities'][1]['time'] = '10:00'
    trip['days'][0]['activities'] += [deepcopy(trip['days'][0]['activities'][1])] * 5
    results = audit_itinerary(trip)
    assert any('overlap' in item for item in results['findings'])
    assert any('too many stops' in item for item in results['findings'])


def test_offline_audit_accepts_saved_quality_report(tmp_path):
    file = tmp_path / 'trip_quality.json'
    file.write_text(json.dumps({'results': [
        {'case_id': 'sample', 'request': 'Plan a trip', 'fields': {'days': 3},
         'itinerary': sample_trip()},
        {'case_id': 'failed', 'status': 'FAIL'},
    ]}), encoding='utf-8')
    audits = audit_file(file)
    assert len(audits) == 1
    assert audits[0]['case'] == 'sample'
    assert isinstance(audits[0]['score'], int)


def test_prompt_requires_bounded_metadata_and_no_fake_verification():
    assert 'why_this_fits' in ITINERARY_REPAIR_SYSTEM
    assert 'arrival_departure_plan' in ITINERARY_REPAIR_SYSTEM
    assert 'planning_assumptions' in ITINERARY_REPAIR_SYSTEM
    assert 'weather_alternative' in ITINERARY_REPAIR_SYSTEM
    assert 'Do not fabricate' in ITINERARY_REPAIR_SYSTEM
    assert 'For REVISE of specified day(s)' in ITINERARY_REPAIR_SYSTEM


def test_not_a_skill_registry_change():
    root = Path(__file__).resolve().parents[2]
    manifest_paths = sorted((root / 'plugins' / 'skills').glob('*/manifest.json'))
    # Never introduce a new Skill as an implicit content-quality prerequisite.
    assert len(manifest_paths) == 18
    assert not any('content_audit' in str(path) for path in manifest_paths)

@pytest.mark.asyncio
async def test_adaptive_refinement_is_opt_in_and_keeps_good_original(monkeypatch):
    from types import SimpleNamespace
    from core.runtime import Runtime

    class BrokenEnhancer:
        async def complete(self, *args, **kwargs):
            return {'itinerary': {'days': []}}

    runtime = Runtime(None, BrokenEnhancer(), None, None,
                      SimpleNamespace(intent_timeout=25), retrieval=object())
    base = validate_itinerary(sample_trip())
    monkeypatch.delenv('POREIAGO_CONTENT_REVIEW_MODE', raising=False)
    result = await runtime._optional_content_refinement(base, 'Plan an art trip', {'days': 3})
    assert result == base
    monkeypatch.setenv('POREIAGO_CONTENT_REVIEW_MODE', 'adaptive')
    result = await runtime._optional_content_refinement(base, 'Plan an art trip', {'days': 3})
    assert result == base


@pytest.mark.asyncio
async def test_adaptive_refinement_accepts_only_valid_measurable_improvement(monkeypatch):
    from types import SimpleNamespace
    from core.runtime import Runtime

    weak = sample_trip()
    for day in weak['days']:
        day.pop('area_cluster')
        day.pop('weather_alternative')
        day.pop('logistics')
        day.pop('notes')
        for activity in day['activities']:
            activity.pop('why_this_fits', None)
            activity.pop('verification_needed', None)
    weak = validate_itinerary(weak)
    better = validate_itinerary(sample_trip())

    class Enhancer:
        async def complete(self, role, system, prompt, json_mode=False):
            assert role == 'reasoning' and json_mode is True
            assert 'quality_gaps' in json.loads(prompt)
            return {'itinerary': better}

    monkeypatch.setenv('POREIAGO_CONTENT_REVIEW_MODE', 'adaptive')
    runtime = Runtime(None, Enhancer(), None, None,
                      SimpleNamespace(intent_timeout=25), retrieval=object())
    chosen = await runtime._optional_content_refinement(weak, 'Plan an art trip with food',
                                                         {'days': 3})
    assert audit_itinerary(chosen, request='Plan an art trip with food')['score'] > (
        audit_itinerary(weak, request='Plan an art trip with food')['score'])


@pytest.mark.asyncio
async def test_adaptive_refinement_cannot_drop_original_preferences(monkeypatch):
    from types import SimpleNamespace
    from core.runtime import Runtime

    weak = sample_trip()
    for day in weak['days']:
        day.pop('area_cluster')
        day.pop('weather_alternative')
        day.pop('logistics')
        day.pop('notes')
        for activity in day['activities']:
            activity.pop('why_this_fits', None)
    weak = validate_itinerary(weak)
    proposal = sample_trip()
    proposal['preferences'] = {'pace': 'rushed'}

    class Enhancer:
        async def complete(self, *_args, **_kwargs):
            return {'itinerary': proposal}

    monkeypatch.setenv('POREIAGO_CONTENT_REVIEW_MODE', 'adaptive')
    runtime = Runtime(None, Enhancer(), None, None,
                      SimpleNamespace(intent_timeout=25), retrieval=object())
    chosen = await runtime._optional_content_refinement(weak, 'Plan an art trip', {'days': 3})
    assert chosen == weak
