"""Regression tests for a short local routing decision and detailed trip generation."""
from dataclasses import replace
from pathlib import Path

import pytest

from aligo.config import settings
from aligo.model_agent import model_first_plan
from aligo.registry import SkillRegistry
from aligo.runtime import Runtime

ROOT = Path(__file__).resolve().parents[1]


class TwoStageModel:
    def __init__(self):
        self.calls = []

    def router_decision_only(self):
        return True

    async def complete(self, role, system, prompt, json_mode=False):
        self.calls.append((role, system, prompt, json_mode))
        if role == 'router':
            return {'mode': 'direct', 'operation': 'create', 'itinerary': None,
                    'answer': '', 'fields': {'destination': 'Kyoto', 'days': 5},
                    'goals': ['balanced local experiences'], 'skills': []}
        if role == 'reasoning':
            return {'itinerary': {'destination': 'Kyoto', 'title': 'Five Days in Kyoto',
                    'days': [
                        {'day': n, 'title': f'Day {n}',
                         'activities': [{'description': f'Visit a historic area on day {n}.'},
                                        {'description': f'Try regional food on day {n}.'}]}
                        for n in range(1, 6)]}}
        raise AssertionError(f'Unexpected model role: {role}')


@pytest.mark.asyncio
async def test_local_router_uses_short_decision_and_repair_generates_complete_trip():
    registry = SkillRegistry(ROOT / 'plugins/skills')
    models = TwoStageModel()
    query = 'Plan a five-day trip to Kyoto.'
    fields = {'destination': 'Kyoto', 'days': 5}
    frame, answer = await model_first_plan(query, [], fields, {}, models, registry, 8)
    assert frame.mode == 'model_direct'
    assert frame.task['operation'] == 'create'
    assert frame.task['itinerary'] is None
    assert answer is None
    assert 'Never generate the day-by-day itinerary' in models.calls[0][1]

    runtime = Runtime(None, models, None, registry,
                      replace(settings, agent_mode='model-first'), retrieval=object())
    itinerary, delta, response = await runtime._prepare_trip_update(
        query, [], None, '', frame, answer, [], {}, frame.fields)
    assert len(itinerary['days']) == 5
    assert 'Kyoto' in response
    assert [call[0] for call in models.calls] == ['router', 'reasoning']


def test_profile_installer_marks_router_decision_only(tmp_path):
    import importlib.util
    import json
    path = ROOT / 'scripts' / 'configure_local_llm.py'
    spec = importlib.util.spec_from_file_location('local_installer_two_stage', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    registry = tmp_path / 'models.json'
    registry.write_text('{"profiles":[]}', encoding='utf-8')
    mod.configure_models(registry)
    profiles = json.loads(registry.read_text(encoding='utf-8'))['profiles']
    assert profiles[0]['decision_only'] is True
    assert profiles[0]['max_output_tokens'] == 3072
    assert profiles[0]['reasoning_effort'] == 'none'
    assert 'reasoning_effort' not in profiles[1]
    assert 'reasoning_effort' not in profiles[2]
    assert profiles[1]['decision_only'] is False
    assert profiles[1]['max_output_tokens'] == 8192
