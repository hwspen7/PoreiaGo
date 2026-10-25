"""Contract regressions for blueprint uniqueness and activity-level repair."""
import json

import pytest

from planning.segmented_planning import (
    _blueprint, _cross_day_issues, activity_repair_response_schema,
    generate_daily_trip,
)
from planning.trip_state import TripValidationError


def alloc(n, anchor):
    return {'day': n, 'city': 'Columbus', 'area': 'Downtown Columbus',
            'theme': 'Local arts and history', 'anchors': [anchor],
            'overnight': 'Downtown Columbus', 'transfer': '',
            'day_type': 'sightseeing'}


def day(n, primary):
    return {'day': n, 'title': f'Columbus day {n}',
            'area_cluster': 'Downtown Columbus',
            'pace': 'balanced',
            'logistics': 'Walk within the local district and use a short taxi transfer.',
            'weather_alternative': 'Explore the downtown indoor arts collection instead.',
            'activities': [
                {'description': primary, 'time': 'Morning',
                 'duration_minutes': 90},
                {'description': f'Try a regional lunch in Columbus on Day {n}',
                 'time': 'Afternoon'},
                {'description': f'Explore a local creative studio on Day {n}',
                 'time': 'Evening'},
            ]}


def test_blueprint_rejects_duplicate_unique_venue_but_allows_distinct_district_experiences():
    plans = [alloc(1, 'Franklin Park Conservatory'),
             alloc(2, 'Columbus Museum of Art'),
             alloc(3, 'Ohio Statehouse'),
             alloc(4, 'Visit Columbus Museum of Art')]
    with pytest.raises(TripValidationError, match='Blueprint repeats anchor'):
        _blueprint({'destination': 'Columbus', 'days': plans},
                   destination='Columbus', count=4)
    plans[3] = alloc(4, 'German Village district')
    result = _blueprint({'destination': 'Columbus', 'days': plans},
                        destination='Columbus', count=4)
    assert len(result['days']) == 4


def test_named_venue_collision_even_when_not_in_blueprint_anchors():
    plans = [alloc(1, 'German Village district'),
             alloc(2, 'Short North district')]
    visits = [day(1, 'Visit Columbus Museum of Art'),
              day(2, 'Tour Columbus Museum of Art')]
    collisions = _cross_day_issues(visits, plans, request='Columbus museums')
    assert 2 in collisions
    assert 'columbus museum of art' in collisions[2][0]
    visits[1]['activities'][0]['description'] = 'Travel past Columbus Museum of Art'
    assert _cross_day_issues(visits, plans, request='Columbus museums') == {}


class DailyModel:
    def __init__(self, *, valid_repair=True):
        self.valid_repair = valid_repair
        self.days_called = []
        self.repair_prompts = []
        self.daily_prompts = []

    async def complete_global_blueprint(self, system, prompt, *, day_count, compact=False):
        return {'blueprint': {'destination': 'Columbus', 'title': 'Three days in Columbus',
                'summary': 'Explore arts and walkable neighborhoods in Columbus.',
                'days': [alloc(1, 'German Village district'),
                         alloc(2, 'Short North district'),
                         alloc(3, 'Ohio Statehouse')]}}

    async def complete_daily_detail(self, system, prompt, *, day_number, compact=False):
        ctx = json.loads(prompt)
        self.days_called.append(day_number)
        self.daily_prompts.append(ctx)
        title = {1: 'Visit Columbus Museum of Art',
                 2: 'Tour Columbus Museum of Art',
                 3: 'Walk through Ohio Statehouse'}[day_number]
        if day_number == 2 and ctx.get('avoid_repeat_visits'):
            title = 'Visit an independent neighborhood art studio'
        return {'days': [day(day_number, title)]}

    async def complete_activity_repair(self, system, prompt, *, day_number):
        assert day_number == 2
        ctx = json.loads(prompt)
        self.repair_prompts.append(ctx)
        if self.valid_repair:
            return {'activity': {'description': 'Visit the historic Ohio Theatre',
                                 'time': 'Morning', 'duration_minutes': 85},
                    'day_updates': {'logistics':
                        'Walk from the theatre to the nearby neighborhood art area.',
                        'next_activity_transport_hint':
                        'Walk from Ohio Theatre to the lunch area.'}}
        return {'activity': {'description': 'Tour Columbus Museum of Art',
                             'time': 'Morning'}, 'day_updates': {}}


async def generate(model):
    return await generate_daily_trip(
        model, request='Plan three days in Columbus for art and food.',
        destination='Columbus', days=3,
        fields={'destination': 'Columbus', 'days': 3}, preferences={})


@pytest.mark.asyncio
async def test_single_activity_repair_preserves_every_other_activity_and_day():
    model = DailyModel()
    result = await generate(model)
    assert model.days_called == [1, 2, 3]
    assert len(model.repair_prompts) == 1
    assert model.repair_prompts[0]['activity_index'] == 0
    assert 'columbus museum of art' in model.repair_prompts[0]['forbidden_venues']
    assert result['days'][1]['activities'][0]['description'] == 'Visit the historic Ohio Theatre'
    assert result['days'][1]['activities'][1]['transport_hint'] == 'Walk from Ohio Theatre to the lunch area.'
    assert result['days'][1]['activities'][1]['description'] == 'Try a regional lunch in Columbus on Day 2'
    assert result['days'][0]['activities'][0]['description'] == 'Visit Columbus Museum of Art'
    assert result['days'][2]['activities'][0]['description'] == 'Walk through Ohio Statehouse'
    assert any(r['venue'] == 'columbus museum of art' for r in model.daily_prompts[1]['confirmed_visit_registry'])


@pytest.mark.asyncio
async def test_repair_missing_day_dependency_update_falls_back_safely():
    class MissingUpdates(DailyModel):
        async def complete_activity_repair(self, *args, **kwargs):
            value = await super().complete_activity_repair(*args, **kwargs)
            value.pop('day_updates', None)
            return value

    model = MissingUpdates()
    result = await generate(model)
    assert model.days_called == [1, 2, 3, 2]
    assert result['days'][1]['activities'][0]['description'] == 'Visit an independent neighborhood art studio'


@pytest.mark.asyncio
async def test_invalid_activity_repair_falls_back_to_bounded_day_repair():
    model = DailyModel(valid_repair=False)
    result = await generate(model)
    assert len(model.repair_prompts) >= 1
    assert model.days_called == [1, 2, 3, 2]
    assert result['days'][1]['activities'][0]['description'] == 'Visit an independent neighborhood art studio'
    assert result['days'][1]['activities'][1]['description'] == 'Try a regional lunch in Columbus on Day 2'


def test_single_activity_schema_is_restricted():
    schema = activity_repair_response_schema()
    assert schema['required'] == ['activity', 'day_updates']
    assert schema['additionalProperties'] is False
    assert schema['properties']['activity']['properties']['cost_level']['enum'] == [
        'free', 'low', 'moderate', 'high', 'unknown']


def test_station_visits_do_not_trigger_blueprint_rejection_or_cross_day_repeat():
    plans = [alloc(1, 'Columbus Union Station'),
             alloc(2, 'Columbus Museum of Art'),
             alloc(3, 'Columbus Union Station')]
    # Named transit gateways may be used more than once in a real trip.
    plans[0]['anchors'] = ['Central train station']
    plans[2]['anchors'] = ['Central train station']
    plan = _blueprint({'destination': 'Columbus', 'days': plans},
                      destination='Columbus', count=3)
    visits = [day(1, 'Visit Central train station'),
              day(2, 'Visit Columbus Museum of Art'),
              day(3, 'Visit Central train station')]
    assert _cross_day_issues(visits, plan['days'], request='Columbus trip') == {}


@pytest.mark.asyncio
async def test_live_wrapper_transfers_single_activity_method_and_logs_phase():
    from evaluation.evaluate_travel_quality import TimedGateway, model_stage_label

    class Gateway:
        async def complete_activity_repair(self, system, prompt, *, day_number):
            assert day_number == 6
            return {'activity': {'description': 'Visit the Ohio Theatre',
                                 'time': 'Morning'}}

        def completion_metadata(self):
            return {'max_output_tokens': 2048, 'completion_tokens': 82}

    wrapper = TimedGateway(Gateway())
    result = await wrapper.complete_activity_repair('system', '{}', day_number=6)
    assert result['activity']['description'] == 'Visit the Ohio Theatre'
    entry = wrapper.role_calls[0]
    assert entry['status'] == 'ok'
    assert model_stage_label(entry) == 'reasoning[Day 6: Activity Repair]'
    assert entry['usage']['max_output_tokens'] == 2048


def test_quality_audit_distinguishes_generic_placeholders_and_budget_evidence():
    from quality.content_audit import audit_itinerary

    weak = {'destination': 'Columbus', 'days': [
        {'day': 1, 'title': 'Art and Food', 'area_cluster': 'Downtown Columbus',
         'logistics': 'Walk to the neighborhood, then use a local bus.',
         'weather_alternative': 'Visit the local museum and other indoor options.',
         'activities': [
             {'description': 'Visit local museum', 'time': 'Morning',
              'verification_needed': 'Check opening hours'},
             {'description': 'Explore nearby shops', 'time': 'Afternoon',
              'verification_needed': 'Check opening hours'},
             {'description': 'Have local dinner', 'time': 'Evening'}]}]}
    strong = {'destination': 'Columbus', 'days': [
        {'day': 1, 'title': 'Art and Food', 'area_cluster': 'Downtown Columbus',
         'logistics': 'Walk from the Statehouse area to the downtown art museum.',
         'weather_alternative': 'Visit Columbus Museum of Art indoor galleries instead.',
         'activities': [
             {'description': 'Tour Columbus Museum of Art collections', 'time': 'Morning',
              'why_this_fits': 'Focused look at Ohio art and creative history.',
              'cost_estimate': 'Indicative USD 20, verify official rates'},
             {'description': 'Browse the historic German Village streets', 'time': 'Afternoon',
              'why_this_fits': 'Distinctive brick architecture and bookstores.'},
             {'description': 'Taste regional food in German Village', 'time': 'Evening',
              'why_this_fits': 'Gives time to sample regional dishes.'}]}]}
    poor = audit_itinerary(weak, request='One day with art, food, and a $100 budget')
    rich = audit_itinerary(strong, request='One day with art, food, and a $100 budget')
    assert poor['dimensions']['experience_actionability'] < rich['dimensions']['experience_actionability']
    assert poor['dimensions']['budget_awareness'] < rich['dimensions']['budget_awareness']
    assert any('Generic activity placeholders' in note for note in poor['findings'])
