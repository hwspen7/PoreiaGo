"""Guard explicit creation requests from short-router intent misclassification."""
import json
from pathlib import Path

import pytest

from intelligence.model_agent import model_first_plan
from services.registry import SkillRegistry
from core.runtime import Runtime, extract_fields
from core.config import settings

ROOT = Path(__file__).resolve().parents[2]


class StubModels:
    def __init__(self, router_response):
        self.router_response = router_response
        self.calls = []

    def router_decision_only(self):
        return True

    async def complete(self, role, system, prompt, json_mode=False):
        self.calls.append((role, json.loads(prompt)))
        if role == 'router':
            return dict(self.router_response)
        if role == 'reasoning':
            return {'itinerary': {
                'destination': 'Japan', 'title': 'A cultural itinerary',
                'days': [
                    {'day': n, 'title': f'Day {n}', 'activities': [
                        {'description': f'Visit local cultural sites on day {n}.'},
                        {'description': f'Try regional food on day {n}.'},
                    ]} for n in range(1, 6)
                ],
            }}
        raise AssertionError(f'Unexpected model role {role}')


@pytest.mark.asyncio
async def test_explicit_five_day_trip_cannot_be_downgraded_to_discussion():
    query = 'Design a relaxed five-day trip in Japan with scenery, culture, and local food.'
    fields = extract_fields(query)
    assert fields['destination'] == 'Japan'
    assert fields['days'] == 5
    models = StubModels({'operation': 'discuss', 'mode': 'clarify',
                         'itinerary': None, 'answer': 'What is your exact budget?',
                         'fields': {}, 'goals': [], 'skills': []})
    registry = SkillRegistry(ROOT / 'plugins/skills')
    frame, answer = await model_first_plan(query, [], fields, {}, models, registry, 8)
    assert frame.task['operation'] == 'create'
    assert frame.mode == 'model_direct'
    assert answer is None
    runtime = Runtime(None, models, None, registry, settings, retrieval=object())
    itinerary, _, _ = await runtime._prepare_trip_update(
        query, [], None, '', frame, answer, [], {}, frame.fields)
    assert len(itinerary['days']) == 5
    assert [call[0] for call in models.calls] == ['router', 'reasoning']


@pytest.mark.asyncio
@pytest.mark.parametrize(('query', 'fields'), [
    ('Plan a five-day trip with museums.', {'days': 5}),
    ('Discuss the best season for a trip in Japan.', {'destination': 'Japan'}),
    ('Plan a budget for my trip in Japan.', {'destination': 'Japan'}),
    ("Don't plan a trip in Japan; explain the weather.", {'destination': 'Japan'}),
    ('Summarize my existing trip in Japan.', {'destination': 'Japan'}),
    ('How do I plan a trip in Japan?', {'destination': 'Japan'}),
])
async def test_genuine_discussion_or_missing_destination_is_not_overridden(query, fields):
    models = StubModels({'operation': 'discuss', 'mode': 'clarify',
                         'itinerary': None, 'answer': 'Please provide more details.',
                         'skills': []})
    registry = SkillRegistry(ROOT / 'plugins/skills')
    frame, answer = await model_first_plan(query, [], fields, {}, models, registry, 8)
    assert frame.task['operation'] == 'discuss'
    assert frame.mode == 'model_clarification'
    assert answer == 'Please provide more details.'
    assert [call[0] for call in models.calls] == ['router']


def test_written_out_day_counts_are_extracted():
    assert extract_fields('Design a relaxed five-day trip in Japan.')['days'] == 5
    assert extract_fields('Make a 12-day itinerary in Japan.')['days'] == 12
