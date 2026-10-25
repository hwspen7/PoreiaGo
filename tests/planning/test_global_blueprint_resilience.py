"""Regression tests for bounded global route planning before daily generation."""
from __future__ import annotations

import json
import sys
import types
from types import SimpleNamespace

import pytest

from intelligence.models import ModelGateway, ModelUnavailable
from planning.segmented_planning import (
    BLUEPRINT_COMPACT_SYSTEM, BLUEPRINT_SYSTEM, blueprint_response_schema,
    generate_daily_trip,
)
from planning.trip_state import TripValidationError
from evaluation.evaluate_travel_quality import TimedGateway, model_stage_label


def blueprint(count: int) -> dict:
    return {'blueprint': {
        'destination': 'Japan', 'title': 'Japan culture route',
        'summary': 'Follow a connected route with overnight bases.',
        'days': [
            {'day': number, 'city': 'Kyoto', 'area': f'District {number}',
             'theme': 'Culture and local food',
             'anchors': [f'Local museum {number}', f'Neighborhood food {number}'],
             'overnight': 'Kyoto Station', 'transfer': 'Local rail',
             'day_type': 'sightseeing'}
            for number in range(1, count + 1)],
    }}


def day(number: int) -> dict:
    return {'days': [{
        'day': number, 'title': f'Day {number} cultural discoveries',
        'area_cluster': f'District {number}',
        'logistics': 'Use local transit between sites and walk nearby streets.',
        'weather_alternative': f'Use a covered cultural venue in District {number}.',
        'activities': [
            {'description': f'Explore neighborhood {number}', 'time': 'Morning'},
            {'description': f'Enjoy local lunch specialties {number}', 'time': 'Afternoon'},
            {'description': f'Visit a craft studio {number}', 'time': 'Evening'},
        ],
    }]}


class BlueprintModel:
    def __init__(self, *, always_fail=False):
        self.calls = []
        self.always_fail = always_fail

    async def complete_global_blueprint(self, system, prompt, *, day_count, compact=False):
        self.calls.append(('blueprint', day_count, compact, system, json.loads(prompt)))
        if self.always_fail or not compact:
            raise ModelUnavailable('ollama-reasoning: OUTPUT_TRUNCATED')
        return blueprint(day_count)

    async def complete_daily_detail(self, system, prompt, *, day_number, compact=False):
        self.calls.append(('day', day_number))
        return day(day_number)


@pytest.mark.asyncio
async def test_truncated_blueprint_retries_compact_before_any_daily_generation():
    model = BlueprintModel()
    result = await generate_daily_trip(
        model, request='Plan an 11-day food and culture trip to Japan.',
        destination='Japan', days=11, fields={'days': 11}, preferences={})
    assert len(result['days']) == 11
    assert [call[0] for call in model.calls] == ['blueprint', 'blueprint'] + ['day'] * 11
    assert model.calls[0][2:4] == (False, BLUEPRINT_SYSTEM)
    assert model.calls[1][2:4] == (True, BLUEPRINT_COMPACT_SYSTEM)
    assert model.calls[1][4]['task'] == 'repair_global_blueprint'
    assert 'OUTPUT_TRUNCATED' in model.calls[1][4]['blueprint_revision_issues'][0]


@pytest.mark.asyncio
async def test_consecutive_blueprint_truncations_fail_without_generating_days():
    model = BlueprintModel(always_fail=True)
    with pytest.raises(TripValidationError, match='Global blueprint failed after bounded retry'):
        await generate_daily_trip(
            model, request='Plan an 11-day Japan trip.', destination='Japan',
            days=11, fields={}, preferences={})
    assert [call[0] for call in model.calls] == ['blueprint', 'blueprint']


def test_blueprint_schema_is_fixed_to_trip_length_and_brief_day_allocations():
    schema = blueprint_response_schema(11)
    plan = schema['properties']['blueprint']
    days = plan['properties']['days']
    assert (days['minItems'], days['maxItems']) == (11, 11)
    entry = days['items']
    assert {'city', 'overnight', 'transfer', 'anchors', 'day_type'} <= set(entry['required'])
    assert entry['properties']['anchors']['maxItems'] == 3
    assert entry['properties']['day_type']['enum'] == [
        'sightseeing', 'arrival', 'departure', 'transfer']
    for key in ('planning_assumptions', 'key_tradeoffs', 'predeparture_checks',
                'arrival_departure_plan', 'budget_strategy'):
        assert key in plan['properties']
    with pytest.raises(ValueError):
        blueprint_response_schema(31)


@pytest.mark.asyncio
@pytest.mark.parametrize('duration,normal,compact', [
    (3, 1536, 1536), (7, 2560, 1920),
    (11, 4096, 3072), (14, 4096, 3072),
    (21, 6144, 4608), (30, 8192, 6144),
])
async def test_real_ollama_global_blueprint_api_is_schema_guided_and_bounded(
        monkeypatch, tmp_path, duration, normal, compact):
    captured = []

    class StubCompletions:
        async def create(self, **kwargs):
            captured.append(kwargs)
            return SimpleNamespace(choices=[SimpleNamespace(
                finish_reason='stop',
                message=SimpleNamespace(content=json.dumps(blueprint(duration))))])

    class StubClient:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=StubCompletions())

    monkeypatch.setitem(sys.modules, 'openai', types.SimpleNamespace(AsyncOpenAI=StubClient))
    monkeypatch.setenv('POREIAGO_OLLAMA_API_KEY', 'ollama')
    cfg = tmp_path / 'models.json'
    cfg.write_text(json.dumps({'profiles': [{'name': 'ollama-reasoning',
        'role': 'reasoning', 'provider': 'openai-compatible',
        'model': 'gemma4:12b-it-qat', 'base_url': 'http://127.0.0.1:11434/v1',
        'api_key_env': 'POREIAGO_OLLAMA_API_KEY', 'max_output_tokens': 8192}]}),
        encoding='utf-8')
    gateway = ModelGateway(SimpleNamespace(models_file=cfg, request_timeout=60))
    await gateway.complete_global_blueprint(BLUEPRINT_SYSTEM, '{}', day_count=duration)
    await gateway.complete_global_blueprint(BLUEPRINT_COMPACT_SYSTEM, '{}',
                                            day_count=duration, compact=True)
    assert [call['max_tokens'] for call in captured] == [normal, compact]
    assert all(call['response_format']['type'] == 'json_schema' for call in captured)
    assert all(call['extra_body']['reasoning_effort'] == 'none' for call in captured)
    assert all(call['response_format']['json_schema']['schema']['properties']
               ['blueprint']['properties']['days']['maxItems'] == duration for call in captured)
    await gateway.complete('reasoning', 'JSON only', '{}', json_mode=True)
    assert captured[-1]['max_tokens'] == 8192
    assert captured[-1]['response_format'] == {'type': 'json_object'}


@pytest.mark.asyncio
async def test_live_timing_wrapper_preserves_blueprint_specialized_call():
    class Gateway:
        def __init__(self):
            self.last_completion = {'max_output_tokens': 4096}
            self.seen = []

        async def complete_global_blueprint(self, system, prompt, *, day_count, compact=False):
            self.seen.append((day_count, compact))
            return blueprint(day_count)

    gateway = Gateway()
    wrapper = TimedGateway(gateway)
    result = await wrapper.complete_global_blueprint(BLUEPRINT_SYSTEM, '{}', day_count=11)
    assert len(result['blueprint']['days']) == 11
    assert gateway.seen == [(11, False)]
    assert wrapper.role_calls[0]['phase'] == 'blueprint'
    assert wrapper.role_calls[0]['usage']['max_output_tokens'] == 4096
    assert wrapper.role_calls[0]['status'] == 'ok'


@pytest.mark.asyncio
async def test_live_timing_wrapper_preserves_failed_blueprint_usage():
    class Gateway:
        last_completion = {'finish_reason': 'length', 'max_output_tokens': 4096}

        async def complete_global_blueprint(self, system, prompt, *, day_count, compact=False):
            raise ModelUnavailable('OUTPUT_TRUNCATED')

    tracked = TimedGateway(Gateway())
    with pytest.raises(ModelUnavailable, match='OUTPUT_TRUNCATED'):
        await tracked.complete_global_blueprint('JSON only', '{}',
                                                day_count=11, compact=True)
    assert tracked.role_calls[0]['phase'] == 'blueprint'
    assert tracked.role_calls[0]['compact'] is True
    assert tracked.role_calls[0]['status'] == 'failed'
    assert tracked.role_calls[0]['usage']['finish_reason'] == 'length'


def test_blueprint_stage_label_is_unambiguous_and_daily_labels_unchanged():
    assert model_stage_label({'role': 'reasoning', 'phase': 'blueprint'}) == 'reasoning[Blueprint]'
    assert model_stage_label({'role': 'reasoning', 'day': 7}) == 'reasoning[Day 7]'
    assert model_stage_label({'role': 'router'}) == 'router'
