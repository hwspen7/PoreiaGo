"""Daily, atomic long-trip generation with reproducible local-model faults."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from aligo.models import ModelUnavailable
from aligo.runtime import Runtime
from aligo.config import settings
from aligo.segmented_planning import generate_daily_trip
from aligo.trip_state import TripValidationError


class DailyModel:
    def __init__(self, truncated_on=None, persistently_bad=None, use_day_wrapper=False):
        self.truncated_on = truncated_on
        self.persistently_bad = persistently_bad
        self.use_day_wrapper = use_day_wrapper
        self.calls = []
        self.attempts = {}

    def daily_planning_enabled(self):
        return True

    async def complete(self, role, system, prompt, json_mode=False):
        assert role == 'reasoning' and json_mode is True
        data = json.loads(prompt)
        self.calls.append(data)
        if 'day_number' not in data:
            return {'blueprint': {
                'destination': 'Columbus',
                'title': 'Neighborhoods and culture in Columbus',
                'summary': 'Keep visits geographically focused and leave time to rest.',
                'arrival_departure_plan': 'Use actual travel arrival and departure times.',
                'days': [{
                    'day': n,
                    'city': 'Columbus',
                    'area': f'Neighborhood {n}',
                    'theme': f'Experience local culture {n}',
                    'anchors': [f'Historical neighborhood {n}'],
                    'overnight': 'Downtown Columbus',
                    'transfer': 'Use nearby stops and ride share as appropriate.',
                    'day_type': 'arrival' if n == 1 else 'departure' if n == data['days'] else 'sightseeing',
                } for n in range(1, data['days'] + 1)],
            }}
        assert len(data['allocated_days']) == 1
        number = data['day_number']
        assert data['allocated_days'][0]['day'] == number
        self.attempts[number] = self.attempts.get(number, 0) + 1
        if number == self.truncated_on and self.attempts[number] == 1:
            raise ModelUnavailable('OUTPUT_TRUNCATED')
        if number == self.persistently_bad:
            raise ModelUnavailable('OUTPUT_TRUNCATED')
        day = {
            'day': number,
            'title': f'Columbus neighborhood day {number}',
            'area_cluster': f'Neighborhood {number}',
            'logistics': 'Walk within the neighborhood; take transit for longer distances.',
            'weather_alternative': 'Replace the outdoor historical tour with an indoor cultural center.',
            'activities': [
                {'description': f'Explore the local architectural history in neighborhood {number}',
                 'time': 'Morning', 'area': f'Neighborhood {number}',
                 'why_this_fits': 'Focus on local city heritage and walkable areas.'},
                {'description': f'Try a Columbus neighborhood lunch on day {number}',
                 'time': 'Afternoon', 'area': f'Neighborhood {number}'},
                {'description': f'Explore a nearby gallery in neighborhood {number}',
                 'time': 'Evening', 'area': f'Neighborhood {number}'},
            ],
        }
        return {'day': day} if self.use_day_wrapper else {'days': [day]}


@pytest.mark.asyncio
@pytest.mark.parametrize('count', [1, 3, 8, 11, 14, 30])
async def test_exactly_one_model_completion_per_day(count):
    model = DailyModel(use_day_wrapper=True)
    itinerary = await generate_daily_trip(model, request='Plan a trip in Columbus with food.',
        destination='Columbus', days=count, fields={'days': count}, preferences={})
    assert len(itinerary['days']) == count
    assert len(model.calls) == count + 1
    assert [call['day_number'] for call in model.calls[1:]] == list(range(1, count + 1))
    assert all(len(call['allocated_days']) == 1 for call in model.calls[1:])
    assert model.attempts == {day: 1 for day in range(1, count + 1)}
    assert all(day['base_city'] == 'Columbus' for day in itinerary['days'])
    assert all(day['overnight_base'] == 'Downtown Columbus' for day in itinerary['days'])


@pytest.mark.asyncio
async def test_truncation_retries_only_one_day_not_whole_trip():
    model = DailyModel(truncated_on=6)
    itinerary = await generate_daily_trip(model, request='Plan eleven days in Columbus.',
        destination='Columbus', days=11, fields={'days': 11}, preferences={})
    assert len(itinerary['days']) == 11
    assert model.attempts[6] == 2
    assert all(model.attempts[day] == 1 for day in range(1, 12) if day != 6)
    assert model.calls[7]['day_number'] == 6
    assert model.calls[7]['task'] == 'repair_one_day'
    assert any('OUTPUT_TRUNCATED' in issue for issue in model.calls[7]['revision_issues'])


@pytest.mark.asyncio
async def test_unrecoverable_day_has_explicit_day_number_and_no_partial_result():
    model = DailyModel(persistently_bad=6)
    with pytest.raises(TripValidationError, match=r'Detail day 6 .*OUTPUT_TRUNCATED'):
        await generate_daily_trip(model, request='Plan eleven days in Columbus.',
            destination='Columbus', days=11, fields={'days': 11}, preferences={})
    assert model.attempts[6] == 2
    assert not any(c.get('day_number', 0) >= 7 for c in model.calls)


@pytest.mark.asyncio
async def test_short_trip_runtime_uses_daily_capability_when_router_did_not_supply_plan():
    model = DailyModel()
    runtime = Runtime(None, model, None, None, settings, retrieval=object())
    frame = SimpleNamespace(task={'operation': 'create', 'itinerary': None})
    itinerary, change, rendered = await runtime._prepare_trip_update(
        'Plan a three-day Columbus culture and food trip.', [], None, '', frame,
        None, [], {}, {'destination': 'Columbus', 'days': 3})
    assert len(itinerary['days']) == 3
    assert change['action'] == 'created'
    assert model.attempts == {1: 1, 2: 1, 3: 1}
    assert 'Day 3' in rendered


@pytest.mark.asyncio
async def test_programming_errors_are_not_disguised_as_day_quality_issues():
    class Buggy(DailyModel):
        async def complete(self, role, system, prompt, json_mode=False):
            if 'day_number' in json.loads(prompt):
                raise RuntimeError('Internal adapter bug')
            return await super().complete(role, system, prompt, json_mode=json_mode)

    with pytest.raises(RuntimeError, match='Internal adapter bug'):
        await generate_daily_trip(Buggy(), request='One day Columbus trip.',
            destination='Columbus', days=1, fields={'days': 1}, preferences={})
