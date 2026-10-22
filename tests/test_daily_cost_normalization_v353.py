"""Model-only recovery of optional price categories in daily itineraries.

Direct/user-supplied itinerary validation remains strict. A model's nonstandard
cost band must never be treated as a factual price or free admission guarantee.
"""
from __future__ import annotations

import json
from copy import deepcopy

import pytest

from aligo.segmented_planning import _normalise_model_day_costs, generate_daily_trip
from aligo.trip_state import TripValidationError, render_itinerary, validate_itinerary


def _day(number: int, cost: object, **extra) -> dict:
    first = {'description': 'Explore a named Kyoto landmark', 'time': 'Morning',
             'cost_level': cost, **extra}
    return {
        'day': number, 'title': f'Kyoto cultural day {number}',
        'area_cluster': 'Central Kyoto',
        'logistics': 'Walk between adjacent sights, or use a local bus for farther places.',
        'weather_alternative': 'Replace an outdoor walk with a Kyoto cultural museum.',
        'activities': [first,
                       {'description': 'Try a regional lunch', 'time': 'Afternoon'},
                       {'description': 'Visit a neighborhood craft district', 'time': 'Evening'}],
    }


@pytest.mark.parametrize('raw,expected', [
    ('free', 'free'), ('FREE', 'free'), ('Free admission', 'free'),
    ('Budget-friendly', 'low'), ('low cost', 'low'), ('medium', 'moderate'),
    ('Mid-range', 'moderate'), ('Expensive', 'high'), ('TBD', 'unknown'),
    ('$$', 'moderate'), ('JPY 1500', 'unknown'), ('¥1,200-2,000', 'unknown'),
    ('guaranteed-free', 'unknown'), ({'amount': 1200}, 'unknown'), (1200, 'unknown'),
])
def test_model_cost_aliases_are_normalized_without_unsupported_price_inferences(raw, expected):
    source = {'days': [_day(1, raw)]}
    initial = deepcopy(source)
    fixed = _normalise_model_day_costs(source)
    assert fixed['days'][0]['activities'][0]['cost_level'] == expected
    assert source == initial
    if isinstance(raw, str) and ('JPY ' in raw or '¥' in raw):
        assert fixed['days'][0]['activities'][0]['cost_estimate'] == raw
    else:
        assert 'cost_estimate' not in fixed['days'][0]['activities'][0]


@pytest.mark.parametrize('bad', ['medium', 'guaranteed-free', 'JPY 1500', {'amount': 5}])
def test_strict_validation_does_not_accept_untrusted_raw_cost_bands(bad):
    with pytest.raises(TripValidationError, match='cost_level'):
        validate_itinerary({'destination': 'Japan', 'days': [_day(1, bad)]})


def test_currency_amount_is_preserved_but_not_misrepresented_as_verified_or_low_cost():
    model_output = {'days': [_day(1, '¥1,500–¥2,000 per person')]}
    fixed = _normalise_model_day_costs(model_output)
    final = validate_itinerary({'destination': 'Japan', 'days': fixed['days']})
    activity = final['days'][0]['activities'][0]
    assert activity['cost_level'] == 'unknown'
    assert activity['cost_estimate'] == '¥1,500–¥2,000 per person'
    display = render_itinerary(final)
    assert 'Estimated cost: ¥1,500–¥2,000 per person (unverified)' in display
    assert 'Cost level: low' not in display
    assert 'Cost level: unknown' not in display


def test_invalid_model_estimate_value_is_discarded_instead_of_causing_a_day_retry():
    raw = {'days': [_day(1, 'moderate', cost_estimate={'amount': 1500})]}
    cleaned = _normalise_model_day_costs(raw)
    assert cleaned['days'][0]['activities'][0] == {
        'description': 'Explore a named Kyoto landmark', 'time': 'Morning',
        'cost_level': 'moderate'}
    assert validate_itinerary({'destination': 'Japan', 'days': cleaned['days']})


class CostVariationModel:
    def __init__(self):
        self.attempts = {}

    async def complete(self, role, system, prompt, json_mode=False):
        assert role == 'reasoning' and json_mode
        context = json.loads(prompt)
        number = context.get('day_number')
        if number is None:
            count = context['days']
            return {'blueprint': {
                'destination': 'Japan', 'title': 'A Japan trip based in Kyoto',
                'summary': 'Explore specific cultural districts and foods.',
                'days': [
                    {'day': n, 'city': 'Kyoto', 'area': 'Central Kyoto',
                     'theme': 'Local food and culture', 'anchors': ['Kyoto craft districts'],
                     'overnight': 'Kyoto', 'transfer': 'Walk or take local transit.',
                     'day_type': 'arrival' if n == 1 else 'departure' if n == count else 'sightseeing'}
                    for n in range(1, count + 1)],
            }}
        self.attempts[number] = self.attempts.get(number, 0) + 1
        values = ['medium', 'JPY 1500', 'free admission', 'budget friendly',
                  'guaranteed-free', 'high', 'variable', '¥800',
                  'low', 'moderate (¥1500)', 'unknown']
        return {'days': [_day(number, values[number - 1])]}


@pytest.mark.asyncio
async def test_eleven_day_trip_survives_nonstandard_cost_levels_without_day_regeneration():
    model = CostVariationModel()
    result = await generate_daily_trip(
        model, request='Plan 11 days in Japan with local food and culture.',
        destination='Japan', days=11, fields={'destination': 'Japan', 'days': 11},
        preferences={},
    )
    assert len(result['days']) == 11
    assert model.attempts == {day: 1 for day in range(1, 12)}
    assert result['days'][1]['activities'][0]['cost_level'] == 'unknown'
    assert result['days'][1]['activities'][0]['cost_estimate'] == 'JPY 1500'
    assert result['days'][4]['activities'][0]['cost_level'] == 'unknown'
    assert result['days'][9]['activities'][0]['cost_level'] == 'moderate'
    assert result['days'][9]['activities'][0]['cost_estimate'] == '¥1500'
