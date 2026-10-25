"""Travel-wide continuity, targeted repair, safe opt-in concurrency and telemetry."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from planning.segmented_planning import (
    _cross_day_issues, _daily_parallelism, _segment_issues,
    generate_daily_trip, planning_tier,
)
from planning.trip_state import TripValidationError
from evaluation.evaluate_travel_quality import TimedGateway


def _day(number, *, venue=None, timeline=None):
    venue = venue or f'Neighborhood cultural experience {number}'
    activities = [
        {'description': f'Visit {venue}', 'time': 'Morning'},
        {'description': f'Taste a different regional lunch on Day {number}', 'time': 'Afternoon'},
        {'description': f'Explore a local district on Day {number}', 'time': 'Evening'},
    ]
    if timeline:
        for item, label in zip(activities, timeline):
            item['time'] = label
    return {'days': [{'day': number, 'title': f'City culture day {number}',
                      'area_cluster': f'Neighborhood {number}',
                      'logistics': 'Explore the assigned neighborhood on foot and use transit between areas.',
                      'weather_alternative': f'Replace the outdoor walk with an indoor art venue on Day {number}.',
                      'activities': activities}]}


class CoordinatedModel:
    def __init__(self, *, total=3, always_duplicate=False, repair_duplicate=False,
                 parallelism=1, chronology=False):
        self.total = total
        self.always_duplicate = always_duplicate
        self.repair_duplicate = repair_duplicate
        self.parallelism = parallelism
        self.chronology = chronology
        self.active = 0
        self.peak = 0
        self.calls = []
        self.attempts = {}

    def daily_concurrency_limit(self):
        return self.parallelism

    async def complete(self, role, system, prompt, json_mode=False):
        data = json.loads(prompt)
        self.calls.append(data)
        return {'blueprint': {'destination': 'Columbus', 'title': 'Columbus arts',
            'summary': 'Move between the downtown districts in a logical order.',
            'days': [{'day': day, 'city': 'Columbus',
                      'area': f'Neighborhood {day}', 'theme': 'Arts and food',
                      'anchors': (["Columbus Museum of Art"] if day == 1 else
                                  ["Franklin Park Conservatory"] if day == 2 else
                                  [f"Neighborhood experience {day}"]),
                      'overnight': 'Downtown Columbus',
                      'transfer': 'Travel by bus or on foot.',
                      'day_type': 'sightseeing'} for day in range(1, data['days'] + 1)]}}

    async def complete_daily_detail(self, system, prompt, *, day_number, compact=False):
        data = json.loads(prompt)
        self.calls.append(data)
        self.attempts[day_number] = self.attempts.get(day_number, 0) + 1
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(0.004)
            if day_number == 1:
                return _day(1, venue='Columbus Museum of Art')
            if day_number == 2 and (self.always_duplicate or
                                    (self.repair_duplicate and self.attempts[2] == 1)):
                return _day(2, venue='Columbus Museum of Art')
            if day_number == 2:
                return _day(2, venue='Franklin Park Conservatory')
            if day_number == 3 and self.chronology and self.attempts[3] == 1:
                return _day(3, timeline=['Evening', 'Morning', 'Afternoon'])
            return _day(day_number)
        finally:
            self.active -= 1


async def create_trip(model, days=3):
    return await generate_daily_trip(model,
        request='Plan a Columbus trip with city museums and arts.',
        destination='Columbus', days=days,
        fields={'destination': 'Columbus', 'days': days}, preferences={})


@pytest.mark.asyncio
async def test_global_route_assignments_reach_each_day_without_large_prior_day_json():
    model = CoordinatedModel()
    result = await create_trip(model)
    assert len(result['days']) == 3
    assert [day['overnight_base'] for day in result['days']] == ['Downtown Columbus'] * 3
    assert model.attempts == {1: 1, 2: 1, 3: 1}
    contexts = [entry for entry in model.calls if 'day_number' in entry]
    assert contexts[1]['previous_accepted_day']['day'] == 1
    assert contexts[2]['next_day'] is None
    assert 'Columbus Museum of Art' in contexts[1]['previously_planned_anchors']
    assert len(contexts[1]['allocated_days']) == 1


@pytest.mark.asyncio
async def test_repeated_named_attraction_repairs_only_later_day():
    model = CoordinatedModel(repair_duplicate=True)
    result = await create_trip(model)
    assert len(result['days']) == 3
    assert model.attempts == {1: 1, 2: 2, 3: 1}
    repair = [context for context in model.calls
              if context.get('task') == 'repair_cross_day_conflict']
    assert len(repair) == 1
    assert repair[0]['day_number'] == 2
    assert 'columbus museum of art' in repair[0]['coordination_issues'][0]
    assert 'Franklin Park Conservatory' in result['days'][1]['activities'][0]['description']


@pytest.mark.asyncio
async def test_persistent_repeat_fails_closed_after_targeted_repair():
    model = CoordinatedModel(always_duplicate=True)
    with pytest.raises(TripValidationError, match='Cross-day consistency failed for Day 2'):
        await create_trip(model)
    assert model.attempts == {1: 1, 2: 5, 3: 1}


def test_cross_day_audit_ignores_repeatable_logistics_and_explicit_return():
    days = [_day(1, venue='Columbus Museum of Art')['days'][0],
            _day(2, venue='Columbus Museum of Art')['days'][0]]
    allocation = [{'day': 1, 'anchors': ['Columbus Museum of Art']},
                  {'day': 2, 'anchors': ['Columbus Museum of Art']}]
    assert 2 in _cross_day_issues(days, allocation, request='City art tour')
    assert _cross_day_issues(days, allocation, request='Revisit Columbus Museum of Art') == {}
    for plan in allocation:
        plan['anchors'] = ['Central station']
    # Named venues now remain detectable even if the blueprint omits them.
    assert 2 in _cross_day_issues(days, allocation, request='A city trip')
    for row in days:
        row['activities'][0]['description'] = 'Travel via Central station'
    assert _cross_day_issues(days, allocation, request='A city trip') == {}


@pytest.mark.asyncio
async def test_optional_parallelism_is_bounded_and_atomic_by_route_group():
    model = CoordinatedModel(total=11, parallelism=2)
    result = await create_trip(model, days=11)
    assert len(result['days']) == 11
    assert 1 < model.peak <= 2
    assert model.attempts == {i: 1 for i in range(1, 12)}
    assert planning_tier(11).segments == ((1, 4), (5, 8), (9, 11))


@pytest.mark.asyncio
async def test_serial_default_preserves_previous_accepted_day():
    model = CoordinatedModel(total=8, parallelism=1)
    await create_trip(model, days=8)
    assert model.peak == 1
    context = next(row for row in model.calls if row.get('day_number') == 7)
    assert context['previous_accepted_day']['day'] == 6


def test_unconfigured_and_invalid_parallelism_fail_safe():
    assert _daily_parallelism(object()) == 1
    assert _daily_parallelism(SimpleNamespace(daily_concurrency_limit=lambda: True)) == 1
    assert _daily_parallelism(SimpleNamespace(daily_concurrency_limit=lambda: 90)) == 3
    assert _daily_parallelism(SimpleNamespace(daily_concurrency_limit=lambda: -4)) == 1


@pytest.mark.asyncio
async def test_backwards_daily_timeline_repairs_day_without_regenerating_other_days():
    model = CoordinatedModel(chronology=True)
    result = await create_trip(model)
    assert len(result['days']) == 3
    assert model.attempts == {1: 1, 2: 1, 3: 2}
    repair = next(row for row in model.calls if row.get('task') == 'repair_one_day')
    assert any('chronological order' in issue for issue in repair['revision_issues'])


def test_light_pace_excessive_durations_are_rejected():
    day = _day(1)['days'][0]
    day['pace'] = 'light'
    for activity in day['activities']:
        activity['duration_minutes'] = 300
    issues = _segment_issues([day], [{'day': 1, 'day_type': 'sightseeing'}], request='Relaxed')
    assert any('daily capacity' in issue for issue in issues)


@pytest.mark.asyncio
async def test_timed_gateway_forwards_schema_bounded_daily_method_and_records_day():
    class Gateway:
        def __init__(self):
            self.calls = []
        def daily_planning_enabled(self):
            return True
        def daily_concurrency_limit(self):
            return 1
        def completion_metadata(self):
            return {'max_output_tokens': 8192, 'finish_reason': 'stop'}
        async def complete_daily_detail(self, system, prompt, *, day_number, compact=False):
            self.calls.append((day_number, compact))
            return _day(day_number)
    gateway = Gateway()
    tracked = TimedGateway(gateway)
    assert tracked.daily_planning_enabled()
    assert tracked.daily_concurrency_limit() == 1
    reply = await tracked.complete_daily_detail('system', '{}', day_number=2)
    assert reply['days'][0]['day'] == 2
    assert gateway.calls == [(2, False)]
    assert tracked.role_calls[0]['day'] == 2
    assert tracked.role_calls[0]['usage']['max_output_tokens'] == 8192


def test_real_model_parallelism_is_explicit_and_clamped(monkeypatch):
    from intelligence.models import ModelGateway
    gateway = object.__new__(ModelGateway)
    for raw, expected in [('1', 1), ('2', 2), ('3', 3), ('20', 3),
                          ('0', 1), ('oops', 1)]:
        monkeypatch.setenv('POREIAGO_DAILY_PARALLELISM', raw)
        assert gateway.daily_concurrency_limit() == expected

@pytest.mark.asyncio
async def test_gateway_usage_is_isolated_between_parallel_day_calls():
    from contextvars import ContextVar

    class Gateway:
        def __init__(self):
            self._usage = ContextVar('test_usage', default=None)
        def completion_metadata(self):
            return self._usage.get()
        async def complete_daily_detail(self, system, prompt, *, day_number, compact=False):
            self._usage.set({'completion_tokens': day_number * 100,
                             'max_output_tokens': 8192})
            await asyncio.sleep(0.003)
            return _day(day_number)

    tracked = TimedGateway(Gateway())
    await asyncio.gather(*(tracked.complete_daily_detail(
        'system', '{}', day_number=n) for n in (1, 2, 3)))
    assert sorted((record['day'], record['usage']['completion_tokens'])
                  for record in tracked.role_calls) == [(1, 100), (2, 200), (3, 300)]


@pytest.mark.asyncio
async def test_legacy_gateway_without_daily_method_uses_standard_completion():
    class Legacy:
        def __init__(self):
            self.last_completion = None
        async def complete(self, role, system, prompt, json_mode=False):
            assert role == 'reasoning' and json_mode
            return _day(2)
    tracked = TimedGateway(Legacy())
    assert not tracked.daily_planning_enabled()
    assert tracked.daily_concurrency_limit() == 1
    result = await tracked.complete_daily_detail('system', '{}', day_number=2)
    assert result['days'][0]['day'] == 2
    assert tracked.role_calls[0]['day'] == 2
