"""Regression tests for conservative request normalization and edit provenance."""
from copy import deepcopy

import pytest

from aligo.activity_revision import merge_activity_patch
from aligo.runtime import extract_fields, Runtime
from aligo.trip_state import validate_itinerary
from aligo.validation_gate import validate_for_commit, ItineraryGateError


def itinerary(days=1):
    return validate_itinerary({'destination': 'Example Region', 'title': 'Example trip',
        'days': [{'day': n, 'title': f'Day {n}', 'base_city': 'Town Center',
                  'activities': [
                      {'description': 'Visit Local Gallery', 'place_name': 'Local Gallery',
                       'start_time': '09:00', 'duration_minutes': 120,
                       'evidence_status': 'pending', 'evidence_url': 'https://example.net/source',
                       'cost_estimate': '15 USD', 'cost_currency': 'USD',
                       'booking_advice': 'Reserve in advance', 'transport_hint': 'Walk 20 minutes',
                       'why_this_fits': 'Local art'},
                      {'description': 'City center walk', 'start_time': '12:00',
                       'duration_minutes': 60},
                  ]} for n in range(1, days + 1)]})


def test_short_trip_has_separate_window_and_clean_destination():
    fields = extract_fields('Please give me a 3 hours trip in Qingdao starts at 8am.')
    assert fields['destination'] == 'Qingdao'
    assert fields['duration_minutes'] == 180
    assert fields['start_time'] == '08:00'
    assert fields['end_time'] == '11:00'
    assert fields['days'] == 1


@pytest.mark.parametrize('text,code', [
    ('Plan a day in Paris budget €500', 'EUR'),
    ('Plan a day in Edinburgh under £300', 'GBP'),
    ('Plan a day in Seoul under 300 USD', 'USD'),
    ('Plan a day in Shanghai budget 300 CNY', 'CNY'),
])
def test_currency_with_nonambiguous_unit(text, code):
    assert extract_fields(text)['currency'] == code


def test_yen_not_misrepresented_as_us_dollars_or_unsourced_currency():
    fields = extract_fields('Plan one day in Beijing under ¥300')
    assert fields['budget'] == 300
    assert fields.get('currency') is None
    assert fields['currency_ambiguous']
    assert set(fields['currency_candidates']) == {'CNY', 'JPY'}


def test_previous_currency_cannot_shadow_new_ambiguous_budget():
    instance = Runtime.__new__(Runtime)
    instance.settings = type('Settings', (), {'memory_window': 20})()
    fields = instance._context('Update my budget to ¥300',
                               [{'role': 'user', 'content': 'Plan under 400 USD'}], {})
    assert fields['budget'] == 300 and 'currency' not in fields


def test_descriptive_edit_keeps_unrelated_fields():
    src = itinerary()
    result = merge_activity_patch(src, {'day': 1, 'operations': [
        {'action': 'edit', 'position': 1, 'activity': {'why_this_fits': 'New explanation'}}]}, day_number=1)
    a = result['days'][0]['activities'][0]
    assert a['place_name'] == 'Local Gallery'
    assert a['evidence_url'] == 'https://example.net/source'
    assert a['why_this_fits'] == 'New explanation'


def test_changed_venue_discards_old_price_booking_source_and_route():
    src = itinerary()
    result = merge_activity_patch(src, {'day': 1, 'operations': [
        {'action': 'edit', 'position': 1,
         'activity': {'description': 'Visit Different Museum',
                      'place_name': 'Different Museum'}}]}, day_number=1)
    a = result['days'][0]['activities'][0]
    for name in ('evidence_url', 'cost_estimate', 'cost_currency', 'booking_advice',
                 'transport_hint', 'why_this_fits'):
        assert name not in a
    assert a['evidence_status'] == 'pending'
    assert src['days'][0]['activities'][0]['cost_estimate'] == '15 USD'


def test_changed_time_invalidates_time_sensitive_evidence_only():
    src = itinerary()
    result = merge_activity_patch(src, {'day': 1, 'operations': [
        {'action': 'edit', 'position': 1,
         'activity': {'start_time': '09:30'}}]}, day_number=1)
    a = result['days'][0]['activities'][0]
    assert a['cost_estimate'] == '15 USD'
    assert a['why_this_fits'] == 'Local art'
    assert 'evidence_url' not in a and 'booking_advice' not in a
    assert 'transport_hint' not in a


def test_cross_day_new_repeat_requires_reconciliation():
    src = itinerary(days=2)
    src['days'][1]['activities'][0]['place_name'] = 'Other Gallery'
    src['days'][1]['activities'][0]['description'] = 'Visit Other Gallery'
    assert validate_for_commit(src)
    edited = deepcopy(src)
    edited['days'][1]['activities'][0]['place_name'] = 'Local Gallery'
    with pytest.raises(ItineraryGateError) as caught:
        validate_for_commit(edited, previous=src)
    assert 'CROSS_DAY_DUPLICATE_NAMED_VENUE' in [v.code for v in caught.value.issues]


def test_same_name_different_city_is_not_cross_day_conflict():
    src = itinerary(days=2)
    src['days'][1]['base_city'] = 'Other City'
    src['days'][1]['activities'][0]['place_name'] = 'Other Gallery'
    changed = deepcopy(src)
    changed['days'][1]['activities'][0]['place_name'] = 'Local Gallery'
    assert validate_for_commit(changed, previous=src)

@pytest.mark.asyncio
async def test_budget_skill_does_not_allocate_ambiguous_yen_as_usd():
    from types import SimpleNamespace
    from importlib import import_module
    handler = import_module('plugins.skills.trip-budget-allocation.handler')
    ctx = SimpleNamespace(fields={'budget': 300, 'currency_ambiguous': True,
                                  'currency_candidates': ['CNY', 'JPY']})
    output = await handler.run(ctx, {'budget': 300})
    assert output.status == 'partial'
    assert output.data['currency_candidates'] == ['CNY', 'JPY']
