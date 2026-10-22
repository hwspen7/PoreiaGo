"""Fast, deterministic checks for global/daily planning and traveler quality."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from aligo.config import settings
from aligo.content_audit import audit_itinerary
from aligo.runtime import Runtime
from aligo.segmented_planning import generate_segmented_trip, planning_tier
from aligo.trip_state import TripValidationError, validate_itinerary, validate_segment_days


def test_all_trip_tiers_have_exact_bounded_coverage():
    expected = {1: ('short', 1), 3: ('short', 1),
                4: ('standard', 1), 7: ('standard', 1),
                8: ('extended', 3), 10: ('extended', 3),
                11: ('extended', 3), 12: ('long', 4),
                14: ('long', 4), 15: ('dynamic', 4),
                21: ('dynamic', 6), 30: ('dynamic', 8)}
    for days, (name, batches) in expected.items():
        tier = planning_tier(days)
        assert tier.name == name
        assert len(tier.segments) == batches
        assert [item for start, end in tier.segments for item in range(start, end+1)] == list(range(1, days+1))
        if days >= 8:
            assert all(1 <= end-start+1 <= 4 for start, end in tier.segments)
    for invalid in (0, 31, 7.5, True):
        with pytest.raises(ValueError):
            planning_tier(invalid)


class SegmentModel:
    def __init__(self, *, city='Columbus', faulty_first=False, bad_day=None):
        self.city, self.faulty_first, self.bad_day = city, faulty_first, bad_day
        self.calls = []
        self.attempts = {}

    async def complete(self, role, system, prompt, json_mode=False):
        assert role == 'reasoning' and json_mode is True
        context = json.loads(prompt)
        self.calls.append(context)
        if 'allocated_days' not in context:
            return {'blueprint': {'destination': self.city,
                    'title': f'An exploration of {self.city}',
                    'summary': 'Distinct neighborhood experiences and realistic transfers.',
                    'planning_assumptions': ['Arrival is assumed to be in the afternoon.'],
                    'arrival_departure_plan': 'Adjust the first and last days to actual travel times.',
                    'key_tradeoffs': ['Choose fewer stops to preserve downtime.'],
                    'predeparture_checks': ['Check opening days for major ticketed museums.'],
                    'budget_strategy': 'Use local dining and public transport where practical.',
                    'days': [{'day': day, 'city': self.city,
                              'area': f'District {day}', 'overnight': self.city,
                              'theme': f'Local life on day {day}',
                              'anchors': [f'Neighborhood discovery {day}'],
                              'transfer': 'Walk within the district.',
                              'day_type': ('arrival' if day == 1 else
                                           'departure' if day == context['days'] else 'sightseeing')}
                             for day in range(1, context['days']+1)]}}
        segment = context['allocated_days']
        first = segment[0]['day']
        self.attempts[first] = self.attempts.get(first, 0) + 1
        if self.faulty_first and first == 5 and self.attempts[first] == 1:
            return {'days': []}
        records = []
        for row in segment:
            day = row['day']
            activities = [{'description': f'Explore the distinctive neighborhood discovery {day}',
                           'time': 'Morning', 'area': row['area'],
                           'why_this_fits': 'A place-specific insight into the district.'},
                          {'description': f'Enjoy a local lunch on day {day}',
                           'time': 'Afternoon', 'area': row['area']},
                          {'description': f'Visit a cultural venue in district {day}',
                           'time': 'Evening', 'area': row['area']}]
            if day == self.bad_day:
                activities = [{'description': 'A short visit'}]
            records.append({'day': day, 'title': f'{self.city} day {day}',
                            'activities': activities,
                            'area_cluster': row['area'],
                            'logistics': 'Walk between nearby stops and use transit for longer trips.',
                            'weather_alternative': 'Visit a local history museum if the outdoor walk is rained out.'})
        return {'days': records}


@pytest.mark.asyncio
@pytest.mark.parametrize('length,expected_calls', [(8, 9), (11, 12), (14, 15), (21, 22)])
async def test_daily_trip_uses_global_blueprint_and_exact_days(length, expected_calls):
    model = SegmentModel()
    trip = await generate_segmented_trip(model, request='Plan a food trip in Columbus.',
                                         destination='Columbus', days=length,
                                         fields={'destination':'Columbus','days':length},
                                         preferences={'pace':'balanced'})
    assert len(trip['days']) == length
    assert trip['destination'] == 'Columbus'
    assert len(model.calls) == expected_calls
    assert all(len(call['allocated_days']) == 1 for call in model.calls[1:])
    assert [call['day_number'] for call in model.calls[1:]] == list(range(1, length + 1))
    assert all(day['area_cluster'] == f"District {day['day']}" for day in trip['days'])
    assert model.calls[1]['next_day'] is not None
    assert model.calls[-1]['previous_day'] is not None


@pytest.mark.asyncio
async def test_bad_segment_repaired_only_once_without_repeating_other_batches():
    model = SegmentModel(faulty_first=True)
    trip = await generate_segmented_trip(model, request='Ten day Columbus food trip.',
                                         destination='Columbus', days=11,
                                         fields={'destination':'Columbus','days':11}, preferences={})
    assert len(trip['days']) == 11
    assert model.attempts[5] == 2
    assert model.attempts[1] == model.attempts[9] == 1
    assert any('revision_issues' in call for call in model.calls)


@pytest.mark.asyncio
async def test_persist_only_after_all_segments_pass_content_gates():
    model = SegmentModel(bad_day=6)
    with pytest.raises(TripValidationError, match='Detail day 6'):
        await generate_segmented_trip(model, request='Eleven-day city food trip.',
                                      destination='Columbus', days=11,
                                      fields={'destination':'Columbus','days':11}, preferences={})
    assert model.attempts[6] == 2
    assert 7 not in model.attempts


@pytest.mark.asyncio
async def test_runtime_long_trip_uses_sequenced_generation_and_validation():
    model = SegmentModel()
    runtime = Runtime(None, model, None, None, settings, retrieval=object())
    frame = SimpleNamespace(task={'operation': 'create', 'itinerary': None})
    trip, change, answer = await runtime._prepare_trip_update(
        'Plan an eleven-day trip in Columbus with food and culture.', [], None,
        '', frame, None, [], {}, {'destination':'Columbus','days':11})
    assert len(trip['days']) == 11
    assert change['action'] == 'created'
    assert 'Day 11' in answer
    assert len(model.calls) == 12


def test_misnumbered_segment_is_never_silently_reindexed():
    with pytest.raises(TripValidationError, match='numbered consecutively'):
        validate_segment_days({'days':[{'day':1,'title':'Wrong day',
                                        'activities':['Visit a neighborhood']}]},
                              first=5, last=5)


def test_traveler_readiness_penalizes_empty_pacing_and_boilerplate():
    days = [{'day':index, 'title':f'Day {index}',
             'activities':[{'description':'Visit the historic district'}],
             'logistics':'See sights nearby.',
             'weather_alternative':'Visit a museum'} for index in range(1, 5)]
    weak = validate_itinerary({'destination':'Columbus','title':'Columbus', 'days':days})
    strong = validate_itinerary({'destination':'Columbus','title':'Columbus',
        'days':[{'day':index,'title':f'Day {index}',
                 'logistics':'Walk through the district and take a taxi if tired.',
                 'weather_alternative':'Replace the park with a named indoor museum near the route.',
                 'activities':[{'description':f'Explore a neighborhood in Columbus day {index}',
                                'time':'Morning'},
                               {'description':f'Lunch with a regional food tasting day {index}',
                                'time':'Afternoon'},
                               {'description':f'Try the local art scene day {index}',
                                'time':'Evening'}]}
                for index in range(1,5)]})
    assert audit_itinerary(weak)['dimensions']['traveler_readiness'] < audit_itinerary(strong)['dimensions']['traveler_readiness']

@pytest.mark.asyncio
async def test_end_to_end_eleven_day_creation_and_single_day_revision(tmp_path):
    from aligo.memory import MemoryStore
    from aligo.registry import SkillRegistry
    from pathlib import Path
    from dataclasses import replace

    class RoutedModel(SegmentModel):
        def __init__(self):
            super().__init__()
            self.router_calls = 0

        def configured(self, role):
            return role in ('router', 'reasoning')

        def router_decision_only(self):
            return True

        async def complete(self, role, system, prompt, json_mode=False):
            if role == 'router':
                self.router_calls += 1
                if self.router_calls == 1:
                    return {'mode': 'direct', 'operation': 'create', 'itinerary': None,
                            'answer': '', 'fields': {'destination':'Columbus','days':11},
                            'skills': [], 'goals': []}
                return {'mode': 'direct', 'operation': 'revise', 'itinerary': None,
                        'answer': '', 'fields': {}, 'skills': [], 'goals': []}
            if 'existing_itinerary' in json.loads(prompt) and json.loads(prompt).get('task') == 'revise':
                return {'itinerary': {'changes': [{
                    'day': 7, 'title': 'A rebalanced day in Columbus',
                    'activities': ['Visit a neighborhood gallery', 'Try a local lunch',
                                   'Take a relaxed architecture walk']}],
                    'planning_assumptions': ['Day 7 now emphasizes neighborhoods over museums.'],
                    'key_tradeoffs': ['One gallery replaces a more tiring route.']}}
            return await super().complete(role, system, prompt, json_mode)

    store = MemoryStore(tmp_path / 'trip.sqlite3')
    try:
        model = RoutedModel()
        registry = SkillRegistry(Path(__file__).resolve().parents[1] / 'plugins/skills')
        runtime = Runtime(store, model, None, registry,
                          replace(settings, agent_mode='model-first'), retrieval=object())
        first = await runtime.execute('traveler', 'columbus',
            'Plan an eleven-day trip in Columbus with regional food, art and history.')
        assert first['active_itinerary']['version'] == 1
        assert first['trip_planning_tier'] == 'extended'
        assert first['model_issue'] is None
        prior = first['active_itinerary']['itinerary']
        assert len(prior['days']) == 11
        edited = await runtime.execute('traveler', 'columbus',
            'Revise Day 7 for more local art and a slower pace, keep every other day unchanged.')
        assert edited['active_itinerary']['version'] == 2
        assert edited['trip_change']['changed_days'] == [7]
        new = edited['active_itinerary']['itinerary']
        assert [new['days'][i] == prior['days'][i] for i in range(11) if i != 6] == [True] * 10
        assert new['key_tradeoffs'] == ['One gallery replaces a more tiring route.']
        assert new['planning_assumptions'] == ['Day 7 now emphasizes neighborhoods over museums.']
        assert model.router_calls == 2
    finally:
        store.close()


def test_relaxed_traveler_can_have_two_unhurried_activities():
    from aligo.segmented_planning import _segment_issues
    day = {'day': 5, 'title':'Easy walking day', 'pace':'light',
           'logistics':'Take a taxi to the neighborhood and walk between stops.',
           'weather_alternative':'Visit the local history museum if it rains.',
           'area_cluster':'City center',
           'activities':[{'description':'Enjoy a relaxed city walk', 'time':'Morning'},
                         {'description':'Have a local lunch', 'time':'Afternoon'}]}
    issues = _segment_issues([day], [{'day':5, 'day_type':'sightseeing'}],
                             request='A relaxed Columbus food trip with limited mobility')
    assert issues == []


def test_global_blueprint_requires_explicit_overnight_transfers():
    from aligo.segmented_planning import _blueprint
    days = [{'day':i, 'city':'Columbus' if i <= 2 else 'Cleveland',
             'overnight':'Columbus' if i <= 2 else 'Cleveland',
             'theme':'City heritage', 'anchors':['Museum visit'],
             'day_type':'sightseeing'} for i in range(1, 9)]
    with pytest.raises(TripValidationError, match='without transfer'):
        _blueprint({'destination':'Ohio', 'days':days}, destination='Ohio', count=8)
    days[2]['transfer'] = 'Travel to Cleveland by train or car, subject to availability.'
    plan = _blueprint({'destination':'Ohio', 'days':days}, destination='Ohio', count=8)
    assert plan['days'][2]['city'] == 'Cleveland'

@pytest.mark.asyncio
async def test_invalid_blueprint_repaired_before_generating_any_segments():
    class RepairBlueprint(SegmentModel):
        def __init__(self):
            super().__init__()
            self.blueprint_attempts = 0

        async def complete(self, role, system, prompt, json_mode=False):
            context = json.loads(prompt)
            result = await super().complete(role, system, prompt, json_mode=json_mode)
            if 'allocated_days' not in context:
                self.blueprint_attempts += 1
                if self.blueprint_attempts == 1:
                    result['blueprint']['days'][1]['anchors'] = []
            return result

    model = RepairBlueprint()
    trip = await generate_segmented_trip(model, request='An 11-day Columbus food trip.',
                                         destination='Columbus', days=11,
                                         fields={'days':11}, preferences={})
    assert model.blueprint_attempts == 2
    assert 'blueprint_revision_issues' in model.calls[1]
    assert len(trip['days']) == 11


@pytest.mark.asyncio
async def test_intercity_transfer_survives_merge_but_local_walking_is_not_labeled_intercity():
    from aligo.trip_state import render_itinerary

    class Cities(SegmentModel):
        async def complete(self, role, system, prompt, json_mode=False):
            result = await super().complete(role, system, prompt, json_mode=json_mode)
            context = json.loads(prompt)
            if 'allocated_days' not in context:
                for row in result['blueprint']['days'][4:]:
                    row['city'] = 'Cleveland'
                    row['overnight'] = 'Cleveland'
                    row['area'] = 'Downtown Cleveland'
                result['blueprint']['days'][4]['transfer'] = 'Travel from Columbus to Cleveland with a realistic transfer buffer.'
            return result

    plan = await generate_segmented_trip(Cities(), request='Ohio city exploration.',
                                         destination='Columbus', days=11,
                                         fields={'days':11}, preferences={})
    assert plan['days'][0]['base_city'] == 'Columbus'
    assert 'intercity_transfer' not in plan['days'][0]
    assert plan['days'][4]['base_city'] == 'Cleveland'
    assert 'Columbus to Cleveland' in plan['days'][4]['intercity_transfer']
    text = render_itinerary(plan)
    assert 'Overnight base: Cleveland' in text
    assert 'Intercity transfer: Travel from Columbus to Cleveland' in text
