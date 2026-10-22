"""Regression cases for transfer buffers in exact and imprecise timelines."""

from copy import deepcopy

from aligo.pipeline_integrity import schedule_invariants
from aligo.segmented_planning import _segment_issues


def _day(first='09:00', second='10:20', first_duration=60, buffer=25):
    return {
        'day': 1, 'title': 'A day of art and food',
        'area_cluster': 'Downtown',
        'logistics': 'Walk between attractions.',
        'weather_alternative': 'Visit a covered gallery.',
        'activities': [
            {'description': 'Visit an art museum', 'time': first,
             'duration_minutes': first_duration},
            {'description': 'Lunch in a local restaurant', 'time': second,
             'duration_minutes': 60, 'transfer_minutes': buffer},
            {'description': 'Explore nearby shops', 'time': '15:00',
             'duration_minutes': 60},
        ]
    }


def _issues(day):
    return _segment_issues([day], [{'day_type': 'sightseeing'}], request='Plan a day')


def test_12_hour_and_embedded_clocks_capture_inbound_buffer():
    for first, second in (
        ('9:00 AM', '10:20 AM'),
        ('Morning at 09:00', 'Late Morning 10:20'),
        ('09:00–10:00', '10:20 AM'),
    ):
        row = _day(first, second)
        assert any('transfer/buffer' in value for value in _issues(row))


def test_unverifiable_daypart_timing_does_not_silently_pass():
    row = _day('Morning', 'Late Morning')
    assert any('cannot verify' in issue and 'transfer/buffer' in issue
               for issue in _issues(row))


def test_unverifiable_duration_does_not_silently_pass():
    row = _day()
    row['activities'][0].pop('duration_minutes')
    assert any('cannot verify' in issue and 'transfer/buffer' in issue
               for issue in _issues(row))


def test_actual_sufficient_gap_is_not_falsely_flagged():
    row = _day(second='11:00', buffer=25)
    assert not any('transfer/buffer' in issue for issue in schedule_invariants(row))
    assert not any('transfer/buffer' in issue for issue in _issues(row))


def test_zero_buffer_can_be_valid_even_with_dayparts():
    row = _day('Morning', 'Afternoon', buffer=0)
    assert not any('transfer/buffer' in issue for issue in _issues(row))


def test_overlap_and_buffer_reported_independently():
    row = _day(second='09:50', buffer=25)
    issues = schedule_invariants(row)
    assert any('transfer/buffer' in issue for issue in issues)
    assert any('overlap' in issue for issue in issues)


def test_inbound_transfer_applies_to_immediately_preceding_event():
    row = _day(first='08:00', second='10:20', first_duration=60, buffer=25)
    row['activities'].insert(1, {
        'description': 'Additional stop', 'time': '10:00', 'duration_minutes': 15
    })
    assert any('transfer/buffer' in issue for issue in _issues(row))
