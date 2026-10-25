"""Regression cases for bounded local-LLM recovery and readable failure reports."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from planning.day_recovery import recover_suggested_schedule, rewrite_internal_reasons
from core.pipeline_integrity import schedule_invariants, internal_reason_issue
from planning.segmented_planning import generate_segmented_trip
from planning.trip_state import TripValidationError
from evaluation.evaluate_travel_quality import save_report


def problematic_day() -> dict:
    return {'day': 5, 'title': 'Craft day', 'area_cluster': 'Old district',
            'logistics': 'Walk between nearby stops.',
            'weather_alternative': 'Visit a nearby indoor museum.',
            'activities': [
                {'description': 'Visit the city art museum', 'time': '09:00',
                 'duration_minutes': 120, 'area': 'Old district'},
                {'description': 'Explore the regional heritage gallery', 'time': '10:50',
                 'duration_minutes': 60, 'area': 'Old district',
                 'why_this_fits': 'Matches the allocated day theme.'},
                {'description': 'Enjoy a downtown lunch', 'time': '13:00',
                 'duration_minutes': 60, 'area': 'Old district'},
            ]}


def test_conservative_forward_adjustment_rechecks_schedule_and_preserves_durations():
    day = problematic_day()
    assert schedule_invariants(day)
    revised, changes = recover_suggested_schedule(day)
    assert revised is not None and changes
    assert revised['activities'][1]['time'] == '11:00'
    assert [a['duration_minutes'] for a in revised['activities']] == [120, 60, 60]
    assert not schedule_invariants(revised)
    assert day['activities'][1]['time'] == '10:50'


def test_recovery_respects_transfer_plus_buffer_and_meal_times():
    day = problematic_day()
    day['activities'][1]['transfer_minutes'] = 20
    day['activities'][1]['buffer_minutes'] = 10
    revised, changes = recover_suggested_schedule(day)
    assert revised is not None and revised['activities'][1]['time'] == '11:30'
    assert changes


def test_recovery_refuses_fixed_and_large_shifts():
    day = problematic_day()
    day['activities'][1]['booking_advice'] = 'Reserved for 10:50'
    assert recover_suggested_schedule(day)[0] is None
    day['activities'][1].pop('booking_advice')
    day['activities'][1]['time'] = '09:10'
    assert recover_suggested_schedule(day)[0] is None


def test_internal_why_rewriting_does_not_delete_it():
    day = problematic_day()
    revised, changes = rewrite_internal_reasons(day)
    assert changes
    assert not internal_reason_issue(revised)
    assert 'heritage gallery' in revised['activities'][1]['why_this_fits']
    assert 'why_this_fits' in revised['activities'][1]


class _ElevenDayModel:
    def __init__(self, *, fail_unrecoverably=False):
        self.calls = []
        self.fail_unrecoverably = fail_unrecoverably

    async def complete(self, role, system, prompt, json_mode=False):
        assert role == 'reasoning'
        query = json.loads(prompt)
        if 'allocated_days' not in query:
            return {'blueprint': {'destination': 'Columbus', 'title': 'Columbus craft trip',
                    'summary': 'City art and local neighborhoods.',
                    'days': [{'day': index, 'city': 'Columbus',
                              'area': f'District {index}', 'theme': f'Crafts {index}',
                              'anchors': [f'Experience {index}'], 'overnight': 'Columbus',
                              'transfer': 'Walk locally', 'day_type': 'sightseeing'}
                             for index in range(1, 12)]}}
        index = query['day_number']
        self.calls.append(index)
        if index == 5:
            record = problematic_day()
            if self.fail_unrecoverably:
                record['activities'][1]['time'] = '09:10'
            return {'days': [record]}
        return {'days': [{'day': index, 'title': f'City crafts day {index}',
                         'area_cluster': f'District {index}',
                         'logistics': 'Walk between neighborhood stops.',
                         'weather_alternative': 'Explore a nearby indoor gallery.',
                         'activities': [
                            {'description': f'Explore neighborhood {index}',
                             'time': 'Morning', 'area': f'District {index}'},
                            {'description': f'Lunch in district {index}',
                             'time': 'Afternoon', 'area': f'District {index}'},
                            {'description': f'Visit art exhibit {index}',
                             'time': 'Evening', 'area': f'District {index}'}]}]}


@pytest.mark.asyncio
async def test_two_failed_model_attempts_recover_day_five_without_regenerating_others():
    model = _ElevenDayModel()
    itinerary = await generate_segmented_trip(
        model, request='Plan eleven days of arts in Columbus',
        destination='Columbus', days=11,
        fields={'destination': 'Columbus', 'days': 11}, preferences={})
    assert len(itinerary['days']) == 11
    assert model.calls.count(5) == 2
    assert model.calls.count(4) == model.calls.count(6) == 1
    assert itinerary['days'][4]['activities'][1]['time'] == '11:00'
    assert not internal_reason_issue(itinerary['days'][4])
    assert any('Planning quality review:' in note for note in itinerary['predeparture_checks'])


@pytest.mark.asyncio
async def test_irrecoverable_day_stays_failure_but_retains_review_only_progress():
    model = _ElevenDayModel(fail_unrecoverably=True)
    with pytest.raises(TripValidationError, match='Detail day 5') as error:
        await generate_segmented_trip(
            model, request='Plan eleven days of arts in Columbus',
            destination='Columbus', days=11,
            fields={'destination': 'Columbus', 'days': 11}, preferences={})
    assert [item['day'] for item in error.value.partial_days] == [1, 2, 3, 4]
    assert error.value.failed_day == 5
    assert error.value.rejected_day['day'] == 5
    assert 6 not in model.calls


def test_report_shows_partial_draft_without_claiming_success(tmp_path: Path):
    result = {'results': [{'model': 'mock', 'trial': 1, 'case_id': 'trip11',
              'status': 'FAIL', 'task_success': False, 'score': 0,
              'passed_structure': False, 'elapsed_seconds': 1.0,
              'role_calls': [], 'warnings': [], 'partial_days': [problematic_day()],
              'failed_day': 6}]}
    path = tmp_path / 'review.json'
    save_report(result, path)
    contents = path.with_suffix('.md').read_text(encoding='utf-8')
    assert 'Incomplete diagnostic draft' in contents
    assert 'Day 5: Craft day' in contents
    assert 'Generation failed at Day 6' in contents
    assert 'No usable itinerary' not in contents

@pytest.mark.asyncio
async def test_runtime_preserves_partial_draft_on_wrapped_structured_failure():
    from types import SimpleNamespace
    from core.config import settings
    from core.runtime import Runtime

    model = _ElevenDayModel(fail_unrecoverably=True)
    runtime = Runtime(None, model, None, None, settings, retrieval=object())
    frame = SimpleNamespace(task={'operation': 'create', 'itinerary': None})
    with pytest.raises(TripValidationError, match='INVALID_STRUCTURED_ITINERARY') as error:
        await runtime._prepare_trip_update(
            'Plan eleven days of arts in Columbus', [], None, '', frame, None,
            [], {}, {'destination': 'Columbus', 'days': 11})
    assert [item['day'] for item in error.value.partial_days] == [1, 2, 3, 4]
    assert error.value.failed_day == 5
    assert error.value.rejected_day['day'] == 5


def test_noncanonical_time_labels_are_not_silently_shifted():
    day = problematic_day()
    day['activities'][0]['time'] = '9:00 AM'
    assert recover_suggested_schedule(day)[0] is None


def test_airport_transfer_is_never_shifted_past_an_overlapping_dinner():
    day = problematic_day()
    day['activities'][1]['activity_type'] = 'transport'
    day['activities'][1]['description'] = 'Airport transfer to catch a flight'
    assert recover_suggested_schedule(day)[0] is None
