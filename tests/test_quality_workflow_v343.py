"""Offline regressions for bounded editing, language routing and honest evaluation."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from aligo.config import settings
from aligo.content_audit import audit_itinerary
from aligo.model_agent import model_first_plan
from aligo.models import ModelUnavailable
from aligo.runtime import Runtime
from aligo.trip_state import validate_itinerary
from scripts.evaluate_travel_quality import DisabledSkillCatalog, evaluate_case


def trip():
    return validate_itinerary({
        'destination': 'Sample Region', 'title': 'Local history and food',
        'preferences': {'pace': 'relaxed'},
        'days': [{'day': i, 'title': f'Day {i} in the historic district',
                  'theme': 'Architecture and cuisine',
                  'area_cluster': 'Historic district',
                  'logistics': 'Stay within the district and use a taxi for longer transfers.',
                  'activities': [
                      {'description': 'Explore an established cultural institution in the district.',
                       'why_this_fits': 'Suitable for the traveler interest in history.',
                       'time': '09:30'},
                      {'description': 'Try a regional meal at an established local restaurant.',
                       'time': '12:30'},
                  ]} for i in range(1, 6)],
    })


class Router:
    def __init__(self, result):
        self.result = result
        self.prompts = []

    def router_decision_only(self):
        return True

    async def complete(self, role, system, prompt, json_mode=False):
        assert role == 'router'
        self.prompts.append(json.loads(prompt))
        return self.result


@pytest.mark.asyncio
async def test_saved_trip_router_is_compact_and_explicit_summary_stays_discussion():
    current = trip()
    current['days'][2]['activities'][0]['verification_needed'] = 'A' * 200
    router = Router({'operation': 'revise', 'mode': 'direct', 'itinerary': None,
                     'answer': '', 'skills': [], 'fields': {}, 'goals': []})
    frame, answer = await model_first_plan(
        'Resume en español el itinerario actualizado. No modifiques el plan.',
        [{'role': 'assistant', 'content': 'B' * 1400}] * 6,
        {'destination': 'Sample Region'}, {}, router, DisabledSkillCatalog(), 8,
        active_trip=current)
    assert frame.task['operation'] == 'discuss'
    assert Runtime._needs_active_trip_answer(answer)
    passed = router.prompts[0]['current_active_itinerary']
    assert passed['days'][2]['title'] == current['days'][2]['title']
    assert isinstance(passed['days'][2]['activities'][0], str)
    assert not passed['days'][2].get('logistics')
    assert len(json.dumps(router.prompts[0])) < 8000


@pytest.mark.asyncio
async def test_one_day_edit_does_not_invoke_second_full_synthesis():
    current = trip()
    calls = []

    class Model:
        async def complete(self, role, system, prompt, json_mode=False):
            calls.append((role, json.loads(prompt)))
            assert role == 'reasoning'
            return {'itinerary': {'changes': [{
                'day': 3, 'title': 'New experiences in the old town',
                'activities': [
                    {'description': 'Explore community design studios.'},
                    {'description': 'Enjoy regional lunch specialties.'},
                ]}]}}

    runtime = Runtime(None, Model(), None, DisabledSkillCatalog(), settings,
                      retrieval=object())
    frame = SimpleNamespace(task={'operation': 'revise', 'itinerary': None})
    updated, delta, answer = await runtime._prepare_trip_update(
        'Make Day 3 more engaging, keep all other days unchanged.', [],
        {'itinerary': current}, '', frame, None, [], current['preferences'],
        {'destination': current['destination'], 'days': 5})
    assert len(calls) == 1
    assert calls[0][1]['target_day'] == 3
    assert calls[0][1]['existing_itinerary']['days'][0]['activities'][0] == {
        'description': current['days'][0]['activities'][0]['description']}
    assert calls[0][1]['existing_itinerary']['days'][2] == current['days'][2]
    assert delta['changed_days'] == [3]
    assert updated['days'][0] == current['days'][0]
    assert '### Day 5:' in answer


@pytest.mark.asyncio
async def test_localized_day_edit_uses_only_changed_day_for_synthesis():
    base = trip()
    seen = []

    class Model:
        async def complete(self, role, system, prompt, json_mode=False):
            seen.append((role, json.loads(prompt)))
            assert role == 'synthesis'
            return ('El día 3 se actualizó con actividades culturales y una comida '
                    'regional. Los demás días siguen igual en el itinerario guardado.')

    runtime = Runtime(None, Model(), None, DisabledSkillCatalog(), settings,
                      retrieval=object())
    frame = SimpleNamespace(task={'operation': 'revise', 'itinerary': {
        'changes': [{'day': 3, 'title': 'New local art and cuisine',
                     'activities': ['Explore local workshops', 'Sample regional food']}],
    }})
    _, delta, answer = await runtime._prepare_trip_update(
        'Modifica el día 3 y conserva los otros días.', [], {'itinerary': base},
        '', frame, '', [], {}, {'destination': base['destination'], 'days': 5})
    assert delta['changed_days'] == [3]
    assert seen[0][0] == 'synthesis'
    assert list(seen[0][1]) == ['request', 'destination', 'changed_days',
                               'changed_preferences', 'output_language']
    assert 'El día 3' in answer


def test_model_preference_list_can_be_saved_without_accepting_nested_objects():
    base = trip()
    candidate = deepcopy(base)
    candidate['preferences']['mobility_needs'] = ['few stairs', 'short transfers']
    normalized = Runtime._normalize_model_preferences(candidate)
    saved = validate_itinerary(normalized)
    assert saved['preferences']['mobility_needs'] == 'few stairs, short transfers'
    candidate['preferences']['mobility_needs'] = {'steps': ['unknown']}
    assert Runtime._normalize_model_preferences(candidate)['preferences']['mobility_needs'] == {
        'steps': ['unknown']}


@pytest.mark.asyncio
async def test_stage_failure_is_degraded_even_when_fallback_trip_succeeds():
    current = trip()

    class Model:
        def router_decision_only(self):
            return True

        async def complete(self, role, system, prompt, json_mode=False):
            if role == 'router':
                return {'operation': 'revise', 'mode': 'direct', 'itinerary': None,
                        'answer': '', 'skills': [], 'fields': {}, 'goals': []}
            if role == 'reasoning':
                return {'itinerary': {'changes': [
                    {'day': 3, 'title': 'Updated day three',
                     'activities': ['Explore an arts district', 'Try regional cuisine']}],
                }}
            raise ModelUnavailable('OUTPUT_TRUNCATED')

    parent = {'itinerary': current, 'history': [], 'delta': None,
              'prior_itinerary': None}
    case = {'id': 'revised', 'kind': 'revise', 'depends_on': 'seed',
            'changed_day': 3,
            'request': 'Refresh this itinerary with new cultural experiences.'}
    result, state = await evaluate_case(case, Model(), DisabledSkillCatalog(),
                                        {'seed': parent})
    assert result['task_success']
    assert result['status'] == 'DEGRADED'
    assert result['execution_success'] is False
    assert result['failed_stages'] == ['synthesis']
    assert result['role_calls'][-1]['issue'] == 'OUTPUT_TRUNCATED'
    assert state['itinerary']['days'][2]['title'] == 'Updated day three'


def test_proxy_distinguishes_missing_dayparts_and_optional_budget():
    base = trip()
    complete = audit_itinerary(base, request='Explore local history')
    sparse = deepcopy(base)
    for day in sparse['days']:
        for activity in day['activities']:
            activity.pop('time', None)
            activity.pop('start_time', None)
    incomplete = audit_itinerary(sparse, request='Explore local history')
    assert complete['dimensions']['budget_awareness'] is None
    assert incomplete['dimensions']['pacing_and_timing'] < complete['dimensions']['pacing_and_timing']
    assert incomplete['score'] < complete['score']


@pytest.mark.parametrize('query,expected', [
    ('Change day three for younger travelers.', 3),
    ('Improve the third day.', 3),
    ('Make Day 3 more interesting.', 3),
    ('Modifica el día tres.', 3),
    ('Change Day 3 and Day 4.', None),
    ('Revise all days, especially the atmosphere.', None),
])
def test_unambiguous_day_scope(query, expected):
    assert Runtime._requested_day(query, trip()) == expected
