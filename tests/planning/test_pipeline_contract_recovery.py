"""Behavioral regressions for the integrated pipeline repair contract."""
from __future__ import annotations

import pytest

from core.pipeline_integrity import schedule_invariants
from planning.segmented_planning import (_segment_issues,
                                      activity_repair_response_schema)
from planning.trip_state import TripValidationError, validate_segment_days
from evaluation import evaluate_travel_quality as quality


def _day():
    return {'day': 1, 'title': 'Art and food in Columbus',
            'area_cluster': 'Downtown Columbus',
            'logistics': 'Walk between nearby stops.',
            'weather_alternative': 'Visit a downtown museum instead.',
            'activities': [
                {'description': 'Explore the art museum', 'time': '09:00',
                 'duration_minutes': 60},
                {'description': 'Eat a local lunch', 'time': '10:20',
                 'duration_minutes': 60, 'transfer_minutes': 20},
                {'description': 'Visit a local gallery', 'time': '13:00',
                 'duration_minutes': 60},
            ]}


def test_transfer_metadata_roundtrips_with_explicit_limits():
    result = validate_segment_days({'days': [_day()]}, first=1, last=1)
    assert result[0]['activities'][1]['transfer_minutes'] == 20
    for bad in (-1, True, 361, 5.4, '20'):
        value = _day()
        value['activities'][1]['transfer_minutes'] = bad
        with pytest.raises(TripValidationError, match='transfer_minutes'):
            validate_segment_days({'days': [value]}, first=1, last=1)


def test_transfer_buffer_beyond_available_gap_rejected():
    value = validate_segment_days({'days': [_day()]}, first=1, last=1)[0]
    assert schedule_invariants(value) == []
    value['activities'][1]['transfer_minutes'] = 25
    issues = _segment_issues([value], [{'day_type': 'sightseeing'}], request='Plan a day')
    assert any('transfer/buffer' in problem for problem in issues)


def test_incoming_transfer_buffer_is_reported_even_when_activities_overlap():
    value = _day()
    value['activities'][1]['time'] = '09:50'
    value['activities'][1]['transfer_minutes'] = 25
    issues = _segment_issues([value], [{'day_type': 'sightseeing'}], request='Plan a day')
    assert any('transfer/buffer' in problem for problem in issues)
    core = schedule_invariants(value)
    assert any('overlap' in problem for problem in core)
    assert any('transfer/buffer' in problem for problem in core)


def test_no_incoming_transfer_does_not_create_duplicate_buffer_error():
    value = _day()
    value['activities'][1]['time'] = '09:50'
    value['activities'][1]['transfer_minutes'] = 0
    issues = schedule_invariants(value)
    assert any('overlap' in problem for problem in issues)
    assert not any('transfer/buffer' in problem for problem in issues)


def test_repair_requires_day_dependencies_in_schema():
    schema = activity_repair_response_schema()
    assert schema['required'] == ['activity', 'day_updates']
    assert schema['additionalProperties'] is False
    assert schema['properties']['day_updates']['additionalProperties'] is False
    assert 'logistics' in schema['properties']['day_updates']['properties']
    assert 'next_activity_transport_hint' in schema['properties']['day_updates']['properties']


@pytest.mark.asyncio
async def test_evaluator_rejects_online_without_integrated_mode():
    with pytest.raises(ValueError, match='integrated'):
        await quality.run_models([], [], mode='model', online=True)


@pytest.mark.asyncio
async def test_evaluator_accepts_explicit_modes_without_models(monkeypatch):
    monkeypatch.setattr(quality, 'installed_models', lambda: set())
    report = await quality.run_models([], [], mode='model', online=False)
    assert report['evaluation_mode'] == 'model_only'
    report = await quality.run_models([], [], mode='integrated', online=False)
    assert report['evaluation_mode'] == 'skill_orchestrated'


def test_human_report_is_utf8_and_contains_traces(tmp_path):
    report = {'results': [{'model':'test', 'trial':1, 'case_id':'route3',
        'status':'PASS', 'score':100, 'passed_structure':True,
        'warnings':[], 'elapsed_seconds':0.1, 'role_calls':[],
        'skill_mode':'skill_orchestrated', 'skill_calls_trace':[
            {'skill':'schedule-conflict-validation', 'status':'ok', 'conflicts':0}],
        'answer':'Day 1 — test itinerary'}]}
    file = tmp_path / 'report.json'
    quality.save_report(report, file)
    result = file.with_suffix('.md').read_text(encoding='utf-8')
    assert 'Day 1 — test itinerary' in result
    assert 'Skill evaluation mode: skill_orchestrated' in result
    assert 'Specialist Skill calls recorded: 1' in result
