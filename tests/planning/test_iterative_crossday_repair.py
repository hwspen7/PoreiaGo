"""Bounded repeated cross-day reconciliation without full-trip regeneration."""
from __future__ import annotations

import json

import pytest

from planning.segmented_planning import (
    _cross_day_repair_rounds, generate_daily_trip,
)
from planning.trip_state import TripValidationError


def _day(number: int, title: str) -> dict:
    return {'day': number, 'title': f'Osaka day {number}',
            'area_cluster': 'Osaka city',
            'logistics': 'Use local rail and walk between nearby sites.',
            'weather_alternative': 'Visit a nearby indoor craft exhibition.',
            'activities': [
                {'description': title, 'time': 'Morning'},
                {'description': f'Taste an Osaka local lunch on Day {number}', 'time': 'Afternoon'},
                {'description': f'Explore shops near the hotel on Day {number}', 'time': 'Evening'},
            ]}


class TwoFailedRepairsThenSuccess:
    def __init__(self, *, never_fix=False):
        self.never_fix = never_fix
        self.attempts: dict[int, int] = {}
        self.contexts: list[dict] = []

    async def complete_global_blueprint(self, system, prompt, *, day_count, compact=False):
        return {'blueprint': {'destination': 'Japan', 'title': 'Osaka days',
            'summary': 'Visit independent cultural sites around Osaka.',
            'days': [{'day': n, 'city': 'Osaka', 'area': 'Osaka',
                      'theme': 'Arts, food, and culture',
                      'anchors': ['Sumiyoshi Taisha' if n == 3 else
                                  ('Osaka cultural workshop' if n == 10 else
                                   f'Unique cultural experience {n}')],
                      'overnight': 'Namba', 'transfer': 'Use nearby local transit.',
                      'day_type': 'sightseeing'} for n in range(1, day_count + 1)]}}

    async def complete_daily_detail(self, system, prompt, *, day_number, compact=False):
        ctx = json.loads(prompt)
        self.contexts.append(ctx)
        count = self.attempts.get(day_number, 0) + 1
        self.attempts[day_number] = count
        if day_number == 3:
            title = 'Visit Sumiyoshi Taisha'
        elif day_number == 10 and (count <= 3 or self.never_fix):
            title = 'Visit Sumiyoshi Taisha again'
        else:
            title = f'Explore unique neighborhood cultural workshop {day_number}'
        return {'days': [_day(day_number, title)]}


async def make_trip(model):
    return await generate_daily_trip(
        model, request='Plan eleven days in Japan with local food and culture.',
        destination='Japan', days=11, fields={'destination': 'Japan', 'days': 11},
        preferences={})


@pytest.mark.asyncio
async def test_third_targeted_attempt_corrects_day_ten_and_preserves_other_dates():
    model = TwoFailedRepairsThenSuccess()
    trip = await make_trip(model)
    assert len(trip['days']) == 11
    assert model.attempts[10] == 4
    assert all(model.attempts[n] == 1 for n in range(1, 12) if n != 10)
    assert 'Sumiyoshi Taisha' in trip['days'][2]['activities'][0]['description']
    assert 'Sumiyoshi Taisha' not in trip['days'][9]['activities'][0]['description']
    repairs = [ctx for ctx in model.contexts
               if ctx.get('task') == 'repair_cross_day_conflict']
    assert [ctx['repair_round'] for ctx in repairs] == [1, 2, 3]
    assert all('sumiyoshi taisha' in ctx['avoid_repeat_visits'] for ctx in repairs)
    assert all(ctx['previous_rejected_day']['day'] == 10 for ctx in repairs)


@pytest.mark.asyncio
async def test_persistent_duplicate_stops_at_bounded_limit(monkeypatch):
    monkeypatch.setenv('POREIAGO_CROSS_DAY_REPAIR_ROUNDS', '2')
    model = TwoFailedRepairsThenSuccess(never_fix=True)
    with pytest.raises(TripValidationError, match='after 2 bounded repair rounds'):
        await make_trip(model)
    assert model.attempts[10] == 3
    assert all(model.attempts[n] == 1 for n in range(1, 12) if n != 10)


def test_repair_round_configuration_is_bounded(monkeypatch):
    for setting, expected in [('1', 1), ('4', 4), ('6', 6), ('500', 6),
                              ('0', 1), ('oops', 4)]:
        monkeypatch.setenv('POREIAGO_CROSS_DAY_REPAIR_ROUNDS', setting)
        assert _cross_day_repair_rounds() == expected

@pytest.mark.asyncio
async def test_invalid_repair_is_not_persisted_and_next_round_can_recover():
    class MalformedThenValid(TwoFailedRepairsThenSuccess):
        async def complete_daily_detail(self, system, prompt, *, day_number, compact=False):
            if day_number == 10 and self.attempts.get(day_number, 0) in (1, 2):
                self.attempts[day_number] += 1
                self.contexts.append(json.loads(prompt))
                return {'days': []}
            return await super().complete_daily_detail(
                system, prompt, day_number=day_number, compact=compact)

    model = MalformedThenValid()
    result = await make_trip(model)
    assert len(result['days']) == 11
    assert model.attempts[10] == 4
    assert 'Sumiyoshi Taisha' not in result['days'][9]['activities'][0]['description']
    assert model.attempts[3] == 1
