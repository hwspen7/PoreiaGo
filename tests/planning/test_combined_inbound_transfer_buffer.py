"""Exact regression for additive incoming travel and safety buffers."""

from copy import deepcopy

import pytest

from core.pipeline_integrity import schedule_invariants
from planning.segmented_planning import _segment_issues, daily_response_schema
from planning.trip_state import TripValidationError, validate_segment_days


def day():
    return {
        'day': 1, 'title': 'Museum, market, and garden',
        'area_cluster': 'City center',
        'logistics': 'Travel between the selected stops.',
        'weather_alternative': 'Visit the covered exhibition.',
        'activities': [
            {'description': 'Visit the history museum', 'time': '09:00',
             'duration_minutes': 120},
            {'description': 'Lunch at the covered market', 'time': '11:30',
             'duration_minutes': 60, 'transfer_minutes': 20,
             'buffer_minutes': 10},
            {'description': 'Explore the botanical garden', 'time': '13:00',
             'duration_minutes': 90},
        ],
    }


def segment_issues(row):
    return _segment_issues([row], [{'day_type': 'sightseeing'}], request='Plan a day')


def test_exact_observed_fixture_transfer_and_buffer_are_additive():
    value = day()
    assert schedule_invariants(value) == []
    assert segment_issues(value) == []
    value['activities'][1]['transfer_minutes'] = 25
    direct = schedule_invariants(value)
    pipeline = segment_issues(value)
    assert any('transfer/buffer' in issue and '35' in issue and
               '30 minutes available' in issue and '5 minutes short' in issue
               for issue in direct)
    assert any('transfer/buffer' in issue for issue in pipeline)


def test_metadata_roundtrip_preserves_both_independent_fields():
    value = day()
    normalized = validate_segment_days({'days': [value]}, first=1, last=1)[0]
    assert normalized['activities'][1]['transfer_minutes'] == 20
    assert normalized['activities'][1]['buffer_minutes'] == 10
    normalized['activities'][1]['transfer_minutes'] = 25
    assert any('transfer/buffer' in issue for issue in segment_issues(normalized))


def test_buffer_alone_can_be_required():
    value = day()
    value['activities'][1]['transfer_minutes'] = 0
    value['activities'][1]['buffer_minutes'] = 35
    assert any('transfer/buffer' in issue for issue in segment_issues(value))


@pytest.mark.parametrize('invalid', [-1, True, '10', 1.5, 181])
def test_buffer_is_validated(invalid):
    value = day()
    value['activities'][1]['buffer_minutes'] = invalid
    with pytest.raises(TripValidationError, match='buffer_minutes'):
        validate_segment_days({'days': [value]}, first=1, last=1)


def test_daily_schema_includes_both_incoming_fields():
    fields = daily_response_schema(1)['properties']['days']['items']['properties']['activities']['items']['properties']
    assert fields['buffer_minutes'] == {'type': 'integer', 'minimum': 0, 'maximum': 180}
    assert fields['transfer_minutes']['maximum'] == 360
