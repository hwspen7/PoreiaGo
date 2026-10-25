"""Offline regression tests for an opt-in, read-only quality evaluation harness."""
from copy import deepcopy
from pathlib import Path

import pytest

from core.config import settings
from quality.quality_evaluation import assess_discussion, assess_itinerary, compare_scores
from services.registry import SkillRegistry
from evaluation.evaluate_travel_quality import (evaluate_case, read_cases,
                                             require_local_gateway, selected_cases)

ROOT = Path(__file__).resolve().parents[2]


def build_trip(days=5):
    return {'destination': 'Japan', 'title': 'Kyoto and Nara: Five Days',
            'preferences': {'pace': 'moderate', 'interests': 'culture and local food'},
            'days': [{'day': number,
                      'title': f'Day {number} in Kyoto',
                      'activities': [
                          {'description': 'Visit a traditional cultural district with a local guide.'},
                          {'description': 'Explore the regional food market and local cuisine.'}]}
                     for number in range(1, days + 1)]}


def test_quality_suite_includes_create_edit_discussion_and_comparative_context():
    cases = read_cases(ROOT / 'benchmarks/travel_quality.json')
    assert len(cases) >= 7
    chosen = selected_cases(cases, quick=True)
    assert {c['id'] for c in chosen} == {
        'japan_create', 'japan_day3_revise', 'japan_spanish_summary',
        'accessibility_create'}
    only = selected_cases(cases, ids={'japan_spanish_summary'})
    assert [c['id'] for c in only] == [
        'japan_create', 'japan_day3_revise', 'japan_spanish_summary']


def test_unknown_quality_case_fails_closed():
    cases = read_cases(ROOT / 'benchmarks/travel_quality.json')
    with pytest.raises(ValueError, match='Unknown cases'):
        selected_cases(cases, ids={'missing'})


def test_quality_metrics_reward_complete_itineraries():
    case = {'kind': 'create', 'days': 5, 'destination': 'Japan',
            'required_terms': ['Kyoto', 'Osaka'],
            'constraint_signals': [['food', 'market'], ['temple', 'cultural']]}
    row = assess_itinerary(case, build_trip())
    assert row['passed_structure']
    assert row['checks']['day_count']
    assert not row['checks']['named_places_mentioned']
    assert 0 < row['score'] < 100


def test_quality_metrics_do_not_hide_missing_day_or_thin_plan():
    case = {'kind': 'create', 'days': 5, 'destination': 'Japan'}
    sparse = build_trip(3)
    for day in sparse['days']:
        day['activities'] = [{'description': 'Visit place'}]
    row = assess_itinerary(case, sparse)
    assert row['checks']['day_count'] is False
    assert row['checks']['two_activities_per_day'] is False
    assert row['checks']['specific_activities'] is False
    assert row['score'] < 65


def test_quality_metrics_detect_unrelated_day_mutation():
    old = build_trip()
    candidate = deepcopy(old)
    candidate['days'][2]['title'] = 'Day 3: New food experience'
    good = assess_itinerary({'kind': 'revise', 'changed_day': 3}, candidate,
                            previous=old)
    assert good['score'] == 100
    candidate['days'][0]['title'] = 'Unrequested change to Day 1'
    bad = assess_itinerary({'kind': 'revise', 'changed_day': 3}, candidate,
                           previous=old)
    assert not bad['checks']['target_day_changed']
    assert not bad['checks']['other_days_unchanged']
    assert bad['score'] < good['score']


def test_quality_metrics_detect_non_scalar_preferences():
    candidate = build_trip()
    candidate['preferences']['nested'] = {'unexpected': 'value'}
    row = assess_itinerary({'kind': 'create', 'days': 5, 'destination': 'Japan'}, candidate)
    assert row['score'] == 0
    assert not row['passed_structure']


def test_discussion_metric_requires_no_mutation():
    class FakeFrame:
        task = {'operation': 'create'}
    row = assess_discussion({'kind': 'discuss'}, FakeFrame(), 'A' * 140)
    assert not row['passed_structure']


def test_cloud_provider_profiles_are_removed_from_in_memory_gateway():
    class Gateway:
        profiles = ([{'name': f'ollama-{role}', 'role': role,
                      'provider': 'openai-compatible',
                      'base_url': 'http://127.0.0.1:11434/v1',
                      'api_key_env': 'POREIAGO_OLLAMA_API_KEY',
                      'decision_only': role == 'router',
                      'reasoning_effort': 'none' if role == 'router' else None}
                     for role in ('router', 'reasoning', 'synthesis')]
                    + [{'name': 'cloud', 'role': 'router',
                        'provider': 'openai-compatible', 'base_url': 'https://example.org/v1',
                        'api_key_env': 'CLOUD_KEY'}])
    instance = require_local_gateway(Gateway())
    assert len(instance.profiles) == 3
    assert not any('example.org' in row['base_url'] for row in instance.profiles)


def test_report_comparison_pairs_only_matching_cases():
    rows = [{'case_id': 'a', 'model': 'first', 'score': 80},
            {'case_id': 'a', 'model': 'second', 'score': 75},
            {'case_id': 'b', 'model': 'first', 'score': 90}]
    result = compare_scores(rows)
    assert result[0]['model_scores'] == {'first': 80, 'second': 75}
    assert result[1]['model_scores'] == {'first': 90}


class FakeLocalModel:
    """Mock the two-stage model while executing the real planner/runtime."""
    def __init__(self):
        self.calls = []

    def router_decision_only(self):
        return True

    async def complete(self, role, system, prompt, json_mode=False):
        import json
        self.calls.append(role)
        data = json.loads(prompt)
        if role == 'router':
            operation = ('revise' if 'Make Day 3' in data['request']
                         else 'discuss' if 'Resume en español' in data['request']
                         else 'create')
            return {'operation': operation, 'mode': 'direct',
                    'itinerary': None, 'answer': (
                        'El itinerario contiene varios días y el tercer día cambió '
                        'para incluir visitas culturales, gastronomía y experiencias '
                        'nuevas para viajeros jóvenes. El resto del viaje permanece '
                        'sin cambios, con el mismo destino y el mismo presupuesto.'
                        if operation == 'discuss' else ''),
                    'fields': {}, 'goals': [], 'skills': []}
        if role == 'reasoning':
            if data.get('task') == 'revise':
                trip = deepcopy(data['existing_itinerary'])
                trip['days'][2]['title'] = 'Day 3: New experiences'
            else:
                trip = build_trip()
                trip['days'][3]['title'] = 'Day 4 in Nara'
            return {'itinerary': trip}
        if role == 'synthesis':
            return 'Day 1 ... Day 2 ... Day 3 ... Day 4 ... Day 5 ... ' + 'Travel plan. ' * 20
        raise AssertionError('Unexpected model role')


@pytest.mark.asyncio
async def test_uses_real_two_stage_planner_without_database_writes():
    cases = read_cases(ROOT / 'benchmarks/travel_quality.json')
    picked = selected_cases(cases, ids={'japan_spanish_summary'})
    models = FakeLocalModel()
    registry = SkillRegistry(settings.extensions_dir)
    saved = {}
    for case in picked:
        result, state = await evaluate_case(case, models, registry, saved)
        assert result['passed_structure'], (case['id'], result['warnings'])
        assert state is not None
        saved[case['id']] = state
    assert [r for r in models.calls if r == 'reasoning'] == ['reasoning', 'reasoning']
    assert saved['japan_day3_revise']['itinerary']['days'][0] == (
        saved['japan_create']['itinerary']['days'][0])
