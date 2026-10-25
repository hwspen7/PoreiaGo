"""Live-planning regression contracts without remote model calls."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from intelligence.model_agent import _explicit_trip_creation
from planning.segmented_planning import _blueprint, generate_segmented_trip
from planning.trip_state import TripValidationError


REQUEST = (
    'Plan an eleven-day trip in Japan, based around Kyoto, Osaka and Nara, '
    'with local food and realistic city transfers.'
)


def allocation(count=11):
    cities = ['Kyoto'] * 5 + ['Osaka'] * 4 + ['Nara'] * (count - 9)
    result = []
    for day, city in enumerate(cities, 1):
        result.append({
            'day': str(day), 'city': city, 'area': 'Central district',
            'theme': 'Heritage and regional food', 'anchors': ['Historic district'],
            'overnight': city, 'day_type': 'SIGHTSEEING',
            'transfer': 'Travel from the previous city and check regional transport.',
        })
    return result


@pytest.mark.parametrize('label', [
    'Japan',
    'Japan (Kyoto, Osaka and Nara)',
    'Japan — Kyoto, Osaka and Nara',
    'Kyoto, Osaka and Nara',
])
def test_explicit_requested_destination_refinement_keeps_user_destination(label):
    result = _blueprint({'destination': label, 'days': allocation()},
                        destination='Japan', request=REQUEST, count=11)
    assert result['destination'] == 'Japan'
    assert result['days'][0]['day'] == 1
    assert result['days'][0]['day_type'] == 'sightseeing'


@pytest.mark.parametrize('label', [
    'South Korea', 'Japan and South Korea', 'Tokyo, Yokohama',
])
def test_blueprint_rejects_unrequested_geographic_scope(label):
    with pytest.raises(TripValidationError, match='changed the requested destination'):
        _blueprint({'destination': label, 'days': allocation()},
                   destination='Japan', request=REQUEST, count=11)


def test_city_destination_must_not_silently_expand_to_state():
    days = allocation()
    for row in days:
        row.update(city='Columbus', overnight='Columbus')
    with pytest.raises(TripValidationError, match='changed the requested destination'):
        _blueprint({'destination': 'Ohio', 'days': days},
                   destination='Columbus', count=11,
                   request='Plan eleven days in Columbus, Ohio, not across the state.')


def test_explicit_city_day_count_works_without_the_word_trip():
    request = ('Plan three days specifically in Columbus, Ohio, with art, '
               'local history, neighborhood walks, and food.')
    assert _explicit_trip_creation(request, {'destination': 'Columbus', 'days': 3})
    assert not _explicit_trip_creation('Plan a budget comparison in Columbus',
                                       {'destination': 'Columbus', 'days': 3})


@pytest.mark.asyncio
async def test_matching_blueprint_does_not_consume_unnecessary_retry():
    class Models:
        def __init__(self):
            self.blueprints = 0

        async def complete(self, role, system, prompt, json_mode=False):
            assert role == 'reasoning' and json_mode
            context = json.loads(prompt)
            if 'allocated_days' not in context:
                self.blueprints += 1
                return {'blueprint': {
                    'destination': 'Japan (Kyoto, Osaka and Nara)',
                    'title': 'A cultural journey across Japan',
                    'days': allocation(),
                }}
            return {'days': [{
                'day': day['day'], 'title': 'Regional culture',
                'area_cluster': day['area'],
                'logistics': 'Use local transit between distinct neighborhoods.',
                'weather_alternative': 'Replace outdoor visits with an indoor museum.',
                'activities': [
                    {'description': 'Explore a local historical district', 'time': 'Morning'},
                    {'description': 'Enjoy regional cuisine at lunch', 'time': 'Afternoon'},
                    {'description': 'Visit a nearby cultural venue', 'time': 'Evening'},
                ],
            } for day in context['allocated_days']]}

    models = Models()
    itinerary = await generate_segmented_trip(
        models, request=REQUEST, destination='Japan', days=11,
        fields={'destination': 'Japan', 'days': 11}, preferences={}, timeout=2)
    assert itinerary['destination'] == 'Japan'
    assert len(itinerary['days']) == 11
    assert models.blueprints == 1


@pytest.mark.asyncio
async def test_live_evaluation_fail_fast_reports_stage_and_error(monkeypatch, capsys):
    from intelligence.models import ModelUnavailable
    from evaluation import evaluate_travel_quality as quality

    class FailingModel:
        last_completion = None

        def router_decision_only(self):
            return True

        async def complete(self, role, system, prompt, json_mode=False):
            if role == 'router':
                return {'operation': 'create', 'mode': 'direct',
                        'itinerary': None, 'answer': '', 'fields': {},
                        'goals': [], 'skills': []}
            raise ModelUnavailable('OUTPUT_TRUNCATED')

    monkeypatch.setattr(quality, 'installed_models', lambda: {'test-local-model'})
    monkeypatch.setattr(quality, 'ModelGateway', lambda settings: FailingModel())
    monkeypatch.setattr(quality, 'require_local_gateway', lambda gateway: gateway)
    cases = [
        {'id': 'failing_city', 'kind': 'create',
         'request': 'Plan three days in Columbus.',
         'fields': {'destination': 'Columbus', 'days': 3}},
        {'id': 'unreached_city', 'kind': 'create',
         'request': 'Plan five days in Kyoto.',
         'fields': {'destination': 'Kyoto', 'days': 5}},
    ]
    result = await quality.run_models(cases, ['test-local-model'],
                                      repeats=3, fail_fast=True)
    assert len(result['results']) == 1
    assert result['results'][0]['status'] == 'FAIL'
    output = capsys.readouterr().out
    assert 'Diagnostic:' in output
    assert 'OUTPUT_TRUNCATED' in output
    assert 'Stages:' in output
    assert 'unreached_city' not in output
