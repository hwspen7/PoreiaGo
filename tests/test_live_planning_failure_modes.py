"""Regression coverage for cloud quota exhaustion and structured trip creation."""
import json
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from aligo.config import settings
from aligo.memory import MemoryStore
from aligo.model_agent import model_first_plan
from aligo.models import ModelGateway, ModelUnavailable
from aligo.registry import SkillRegistry
from aligo.runtime import Runtime
from aligo.trip_state import validate_itinerary

ROOT = Path(__file__).resolve().parents[1]


def trip():
    return {
        'destination': 'Japan', 'title': 'A five-day cultural visit',
        'days': [
            {'day': f'Day {index}', 'title': f'Area {index}',
             'activities': [{'activity': f'Enjoy a local museum {index}'},
                            {'text': f'Visit a neighborhood market {index}'}]}
            for index in range(1, 6)
        ],
    }


class FakeModels:
    def __init__(self, router, reasoning=None, synthesis=None):
        self.router = router
        self.reasoning = reasoning
        self.synthesis = synthesis
        self.calls = []
        self.prompts = []

    def configured(self, role):
        return role in {'router', 'reasoning', 'synthesis'}

    async def complete(self, role, system, prompt, json_mode=False):
        self.calls.append(role)
        self.prompts.append(prompt)
        value = getattr(self, role)
        if isinstance(value, Exception):
            raise value
        if value is None:
            raise AssertionError(f'Unexpected call to {role}')
        return value


async def exercise(tmp_path, models, messages):
    store = MemoryStore(tmp_path / 'live_planning.sqlite3')
    registry = SkillRegistry(ROOT / 'plugins/skills')
    runtime = Runtime(store, models, object(), registry,
                      replace(settings, agent_mode='model-first'), retrieval=object())
    try:
        return [await runtime.execute('traveler', 'holiday', message) for message in messages]
    finally:
        store.close()


@pytest.mark.asyncio
async def test_compact_first_trip_creates_version_without_second_model_call(tmp_path):
    models = FakeModels({'mode': 'direct', 'operation': 'create', 'itinerary': trip(),
                         'answer': '', 'skills': []})
    result = (await exercise(tmp_path, models, ['Design a five-day Japan trip.']))[0]
    assert result['active_itinerary']['version'] == 1
    assert result['active_itinerary']['itinerary']['days'][0]['day'] == 1
    assert '### Day 5' in result['answer']
    assert models.calls == ['router']
    assert result['model_issue'] is None


@pytest.mark.asyncio
async def test_invalid_router_trip_and_rate_limited_repair_reports_correct_issue(tmp_path):
    models = FakeModels({'mode': 'direct', 'operation': 'create', 'itinerary': None,
                         'answer': '', 'skills': []},
                        reasoning=ModelUnavailable('groq-reasoning: HTTP 429'))
    result = (await exercise(tmp_path, models, ['Design a five-day trip in Japan.']))[0]
    assert result['active_itinerary'] is None
    assert result['model_issue'] == 'RATE_LIMITED_HTTP_429'
    assert 'HTTP 429' in result['answer']
    assert models.calls == ['router', 'reasoning']


@pytest.mark.asyncio
async def test_router_rate_limit_not_misreported_as_a_trip_success(tmp_path):
    models = FakeModels(ModelUnavailable('groq-router: HTTP 429'))
    result = (await exercise(tmp_path, models, ['Create a relaxed itinerary.']))[0]
    assert result['model_issue'] == 'RATE_LIMITED_HTTP_429'
    assert result['active_itinerary'] is None
    assert 'rate limit' in result['answer'].lower()
    assert models.calls == ['router']


@pytest.mark.asyncio
async def test_revision_keeps_valid_state_even_if_synthesis_is_rate_limited(tmp_path):
    store = MemoryStore(tmp_path / 'live_planning.sqlite3')
    try:
        previous = validate_itinerary(trip())
        store.save_itinerary('traveler', 'holiday', previous)
    finally:
        store.close()
    revision = {'changes': [{'day': 3, 'title': 'Neighborhood art and food',
                             'activities': ['Visit a gallery', 'Sample local cuisine']}]}
    models = FakeModels({'mode': 'direct', 'operation': 'revise',
                         'itinerary': revision, 'answer': '', 'skills': []},
                        synthesis=ModelUnavailable('groq-synthesis: HTTP 429'))
    result = (await exercise(tmp_path, models, ['Change only day 3.']))[0]
    assert result['active_itinerary']['version'] == 2
    assert result['trip_change']['changed_days'] == [3]
    assert result['active_itinerary']['itinerary']['days'][0] == previous['days'][0]
    assert '### Day 3' in result['answer']


@pytest.mark.asyncio
async def test_active_trip_context_does_not_duplicate_legacy_answer(tmp_path):
    registry = SkillRegistry(ROOT / 'plugins/skills')
    models = FakeModels({'mode': 'direct', 'operation': 'discuss',
                         'answer': 'A concise trip summary.', 'skills': []})
    long_history = [{'role': 'user', 'content': 'test ' * 2500} for _ in range(12)]
    await model_first_plan('Summarize the current trip', long_history, {}, {}, models,
                           registry, 8, active_trip=validate_itinerary(trip()),
                           previous_itinerary='LONG_LEGACY_DUPLICATE ' * 500)
    assert models.calls == ['router']
    payload = json.loads(models.prompts[0])
    assert payload['conversation_recovery_context'] == ''
    assert len(json.dumps(payload)) < 19000
    # Payload must stay bounded even for a long conversation.
    from aligo.model_agent import _slim_history
    slim = _slim_history(long_history)
    assert len(slim) <= 6
    assert all(len(item['content']) <= 1400 for item in slim)


@pytest.mark.asyncio
@pytest.mark.parametrize('finish_reason,raw,expected', [
    ('length', '{"mode":"direct"}', 'OUTPUT_TRUNCATED'),
    ('stop', '{invalid json', 'INVALID_MODEL_JSON'),
])
async def test_model_gateway_reports_safe_structural_error(monkeypatch, finish_reason, raw, expected):
    monkeypatch.setenv('GROQ_API_KEY', 'test_token_only')
    monkeypatch.setitem(sys.modules, 'openai', SimpleNamespace(AsyncOpenAI=lambda **kwargs: None))
    models = ModelGateway(settings)

    class FakeCompletions:
        async def create(self, **kwargs):
            return SimpleNamespace(choices=[SimpleNamespace(
                finish_reason=finish_reason,
                message=SimpleNamespace(content=raw))])

    models._clients['groq-router'] = SimpleNamespace(
        chat=SimpleNamespace(completions=FakeCompletions()))
    with pytest.raises(ModelUnavailable) as exc:
        await models.complete('router', 'system', 'query', json_mode=True)
    assert expected in str(exc.value)
    assert 'test_token_only' not in str(exc.value)
