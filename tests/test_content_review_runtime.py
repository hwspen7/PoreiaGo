"""Exercise adaptive content review end-to-end without ambient configuration."""
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest

from aligo.config import settings
from aligo.content_audit import audit_itinerary
from aligo.memory import MemoryStore
from aligo.models import ModelUnavailable
from aligo.registry import SkillRegistry
from aligo.runtime import Runtime
from aligo.trip_state import validate_itinerary
from test_content_intelligence_v342 import sample_trip

ROOT = Path(__file__).resolve().parents[1]


def _weak_trip():
    result = deepcopy(sample_trip())
    for day in result['days']:
        for key in ('area_cluster', 'weather_alternative', 'logistics', 'notes'):
            day.pop(key, None)
        for activity in day['activities']:
            activity.pop('why_this_fits', None)
            activity.pop('verification_needed', None)
    return validate_itinerary(result)


class ReviewModels:
    def __init__(self, itinerary, refinement=None):
        self.itinerary = itinerary
        self.refinement = refinement
        self.calls = []

    def configured(self, role):
        return role in {'router', 'reasoning'}

    async def complete(self, role, system, prompt, json_mode=False):
        self.calls.append(role)
        if role == 'router':
            return {'mode': 'direct', 'operation': 'create',
                    'itinerary': self.itinerary, 'answer': '', 'skills': []}
        if role == 'reasoning':
            if isinstance(self.refinement, Exception):
                raise self.refinement
            return {'itinerary': self.refinement}
        raise AssertionError(f'Unexpected model role: {role}')


async def _run(tmp_path, models):
    store = MemoryStore(tmp_path / 'adaptive_review.sqlite3')
    try:
        runtime = Runtime(store, models, None,
                          SkillRegistry(ROOT / 'plugins/skills'),
                          replace(settings, agent_mode='model-first'),
                          retrieval=object())
        result = await runtime.execute(
            'traveler', 'session', 'Plan a three-day art and food trip.',
            context={'destination': 'Example Region', 'days': 3})
        persisted = store.active_itinerary('traveler', 'session')
        return result, persisted
    finally:
        store.close()


@pytest.mark.asyncio
async def test_adaptive_review_improves_and_persists_valid_plan(tmp_path, monkeypatch):
    monkeypatch.setenv('ALIGO_CONTENT_REVIEW_MODE', 'adaptive')
    weak = _weak_trip()
    strong = validate_itinerary(sample_trip())
    assert audit_itinerary(weak)['score'] + 8 <= audit_itinerary(strong)['score']
    models = ReviewModels(weak, strong)
    result, persisted = await _run(tmp_path, models)
    assert models.calls == ['router', 'reasoning']
    assert result['active_itinerary']['version'] == 1
    assert result['active_itinerary'] == persisted
    assert persisted['itinerary'] == strong
    assert result['model_issue'] is None
    assert 'Museum district' in result['answer']


@pytest.mark.asyncio
@pytest.mark.parametrize('refinement', [
    ModelUnavailable('Optional model failed'),
    {'destination': 'Different Region', 'days': []},
])
async def test_adaptive_review_rejects_bad_candidate_and_keeps_original(
        tmp_path, monkeypatch, refinement):
    monkeypatch.setenv('ALIGO_CONTENT_REVIEW_MODE', 'adaptive')
    weak = _weak_trip()
    models = ReviewModels(weak, refinement)
    result, persisted = await _run(tmp_path, models)
    assert models.calls == ['router', 'reasoning']
    assert persisted['itinerary'] == weak
    assert result['active_itinerary'] == persisted
    assert result['active_itinerary']['version'] == 1
    assert result['model_issue'] is None


@pytest.mark.asyncio
async def test_adaptive_review_skips_good_plan_without_extra_request(
        tmp_path, monkeypatch):
    monkeypatch.setenv('ALIGO_CONTENT_REVIEW_MODE', 'adaptive')
    strong = validate_itinerary(sample_trip())
    assert audit_itinerary(strong)['score'] >= 72
    models = ReviewModels(strong)
    result, persisted = await _run(tmp_path, models)
    assert models.calls == ['router']
    assert result['active_itinerary'] == persisted
    assert persisted['itinerary'] == strong
