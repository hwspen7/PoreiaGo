"""Destination handling and live diagnostic parity, without remote requests."""
from dataclasses import replace
from pathlib import Path

import pytest

from aligo.config import settings
from aligo.memory import MemoryStore
from aligo.model_agent import model_first_plan
from aligo.registry import SkillRegistry
from aligo.runtime import Runtime, extract_fields
from aligo.trip_state import TripValidationError, validate_itinerary


ROOT = Path(__file__).resolve().parents[1]


def example_trip(destination=None):
    payload = {
        'title': 'A relaxed five-day trip',
        'days': [
            {'day': i, 'title': f'Day {i} cultural visits',
             'activities': [{'description': 'Enjoy a local neighborhood'},
                            {'description': 'Try regional food'}]}
            for i in range(1, 6)
        ],
    }
    if destination is not None:
        payload['destination'] = destination
    return payload


def test_destination_only_falls_back_to_explicit_known_context():
    itinerary = validate_itinerary(example_trip(), destination_hint='Japan')
    assert itinerary['destination'] == 'Japan'
    with pytest.raises(TripValidationError, match='Destination'):
        validate_itinerary(example_trip())
    with pytest.raises(TripValidationError, match='Destination'):
        validate_itinerary(example_trip(), destination_hint='')
    assert validate_itinerary(example_trip('Spain'), destination_hint='Japan')['destination'] == 'Spain'


class FakeModels:
    def __init__(self, plan, repair=None):
        self.plan = plan
        self.repair = repair
        self.calls = []

    def configured(self, role):
        return role in ('router', 'reasoning')

    async def complete(self, role, system, prompt, json_mode=False):
        self.calls.append(role)
        if role == 'router':
            return self.plan
        if role == 'reasoning':
            return {'itinerary': self.repair}
        raise AssertionError('Unexpected synthesis call')


@pytest.mark.asyncio
async def test_create_trip_with_missing_model_destination_is_persisted_from_user_query(tmp_path):
    store = MemoryStore(tmp_path / 'trip.sqlite3')
    try:
        models = FakeModels({'mode': 'direct', 'operation': 'create',
                             'itinerary': example_trip(), 'answer': '', 'skills': [],
                             'fields': {'destination': 'Spain'}})
        runtime = Runtime(store, models, None, SkillRegistry(ROOT / 'plugins/skills'),
                          replace(settings, agent_mode='model-first'), retrieval=object())
        result = await runtime.execute('user', 'session',
                                       'Design a relaxed five-day trip in Japan with culture and food.')
        assert result['active_itinerary']['itinerary']['destination'] == 'Japan'
        assert result['active_itinerary']['version'] == 1
        assert result['model_issue'] is None
        assert models.calls == ['router']
    finally:
        store.close()


@pytest.mark.asyncio
async def test_model_field_supplies_destination_when_extraction_does_not():
    registry = SkillRegistry(ROOT / 'plugins/skills')
    models = FakeModels({'mode': 'direct', 'operation': 'create',
                         'itinerary': example_trip(), 'answer': '', 'skills': [],
                         'fields': {'destination': 'Portugal'}})
    frame, answer = await model_first_plan('Plan five days abroad.', [], {}, {}, models,
                                           registry, 8)
    runtime = Runtime(None, models, None, registry, settings, retrieval=object())
    itinerary, change, output = await runtime._prepare_trip_update(
        'Plan five days abroad.', [], None, '', frame, answer, [], {}, frame.fields)
    assert itinerary['destination'] == 'Portugal'
    assert change['action'] == 'created'
    assert 'Day 5' in output


@pytest.mark.asyncio
async def test_diagnostic_uses_live_runtime_path_without_database(monkeypatch, capsys):
    from scripts import diagnose_model_planning as diag
    models = FakeModels({'mode': 'direct', 'operation': 'create',
                         'itinerary': example_trip(), 'answer': '', 'skills': []})
    monkeypatch.setattr(diag, 'ModelGateway', lambda cfg: models)
    result = await diag.diagnose('Design a relaxed five-day trip in Japan with local cuisine.')
    text = capsys.readouterr().out
    assert result == 0
    assert 'PASSED: structured itinerary with 5 day(s).' in text
    assert 'Destination: Japan' in text
    assert models.calls == ['router']


@pytest.mark.asyncio
async def test_diagnostic_exercises_reasoning_repair_when_days_invalid(monkeypatch, capsys):
    from scripts import diagnose_model_planning as diag
    models = FakeModels({'mode': 'direct', 'operation': 'create',
                         'itinerary': {'days': []}, 'answer': '', 'skills': []},
                        repair=example_trip())
    monkeypatch.setattr(diag, 'ModelGateway', lambda cfg: models)
    result = await diag.diagnose('Design a relaxed five-day trip in Japan with local cuisine.')
    text = capsys.readouterr().out
    assert result == 0
    assert 'Destination: Japan' in text
    assert models.calls == ['router', 'reasoning']
