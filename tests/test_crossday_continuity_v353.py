"""A named district may recur; scheduled venue visits must respect route ownership."""
from __future__ import annotations

import json

import pytest

from aligo.segmented_planning import _cross_day_issues, generate_daily_trip
from aligo.trip_state import TripValidationError


def _day(number: int, first_activity: str):
    return {'day': number, 'title': f'Neighborhood day {number}',
            'area_cluster': 'Assigned neighborhood',
            'logistics': 'Use local trains and short walks between named stops.',
            'weather_alternative': 'Visit an indoor cultural exhibition instead of an outdoor walk.',
            'activities': [
                {'description': first_activity, 'time': 'Morning'},
                {'description': f'Sample a local specialty meal for day {number}', 'time': 'Afternoon'},
                {'description': f'Browse independent artisan shops on day {number}', 'time': 'Evening'},
            ]}


def _alloc(number, *anchors):
    return {'day': number, 'city': 'Osaka', 'area': 'Central Osaka',
            'theme': 'Local culture', 'anchors': list(anchors),
            'overnight': 'Namba', 'transfer': 'Metro or walking',
            'day_type': 'sightseeing'}


def test_shinsekai_district_can_support_distinct_experiences_on_different_days():
    days = [_day(2, 'Photograph Shinsekai district architecture and colorful storefronts'),
            _day(10, 'Taste kushikatsu street food in Shinsekai district')]
    allocation = [_alloc(2, 'Shinsekai district'), _alloc(10, 'Shinsekai district')]
    assert _cross_day_issues(days, allocation, request='Plan an Osaka city trip') == {}


def test_same_generic_shinsekai_walk_on_two_days_is_still_detected():
    days = [_day(2, 'Explore Shinsekai district'), _day(10, 'Visit Shinsekai district')]
    allocation = [_alloc(2, 'Shinsekai district'), _alloc(10, 'Shinsekai district')]
    issues = _cross_day_issues(days, allocation, request='Plan an Osaka city trip')
    assert list(issues) == [10]
    assert 'shinsekai district' in issues[10][0]


def test_repeated_named_museum_is_blocked_even_when_experiences_differ():
    days = [_day(2, 'Tour the Columbus Museum of Art galleries'),
            _day(10, 'Visit Columbus Museum of Art for paintings')]
    allocation = [_alloc(2, 'Columbus Museum of Art'),
                  _alloc(10, 'Columbus Museum of Art')]
    assert 10 in _cross_day_issues(days, allocation, request='Columbus vacation')


def test_blueprint_owner_on_later_date_is_preserved_over_earlier_stray_visit():
    days = [_day(2, 'Visit Columbus Museum of Art'),
            _day(10, 'Tour Columbus Museum of Art')]
    allocation = [_alloc(2, 'Other assigned experience'),
                  _alloc(10, 'Columbus Museum of Art')]
    issues = _cross_day_issues(days, allocation, request='Columbus vacation')
    assert list(issues) == [2]
    assert 'from Day 10' in issues[2][0]


def test_transport_or_adjacent_attraction_mentions_do_not_count_as_visits():
    days = [_day(2, 'Visit Columbus Museum of Art'),
            _day(10, 'Take the bus past Columbus Museum of Art to a local gallery')]
    allocation = [_alloc(2, 'Columbus Museum of Art'),
                  _alloc(10, 'Other art venues')]
    assert _cross_day_issues(days, allocation, request='Columbus art trip') == {}
    days[1]['activities'][0]['description'] = 'Lunch near Columbus Museum of Art at a different cafe'
    assert _cross_day_issues(days, allocation, request='Columbus art trip') == {}


def test_explicit_revisit_only_exempts_the_named_place():
    days = [_day(1, 'Visit Columbus Museum of Art'),
            _day(2, 'Tour Columbus Museum of Art'),
            _day(3, 'Walk inside Franklin Park Conservatory'),
            _day(4, 'Visit Franklin Park Conservatory')]
    allocation = [_alloc(1, 'Columbus Museum of Art'),
                  _alloc(2, 'Columbus Museum of Art'),
                  _alloc(3, 'Franklin Park Conservatory'),
                  _alloc(4, 'Franklin Park Conservatory')]
    issues = _cross_day_issues(days, allocation,
                               request='Please revisit Columbus Museum of Art')
    assert 2 not in issues
    assert 4 in issues


class RepairConflictModel:
    """Reproduces an early visit to a venue reserved by the global blueprint."""

    def __init__(self, *, honor_ban=True):
        self.honor_ban = honor_ban
        self.contexts = []
        self.attempts = {}

    async def complete_global_blueprint(self, system, prompt, *, day_count, compact=False):
        return {'blueprint': {'destination': 'Columbus', 'title': 'Columbus city museums',
                'summary': 'Spend three days with local culture, art and parks.',
                'days': [
                    _alloc(1, 'Neighborhood art walk'),
                    _alloc(2, 'Local makers market'),
                    _alloc(3, 'Columbus Museum of Art'),
                ]}}

    async def complete_daily_detail(self, system, prompt, *, day_number, compact=False):
        ctx = json.loads(prompt)
        self.contexts.append(ctx)
        self.attempts[day_number] = self.attempts.get(day_number, 0) + 1
        if day_number == 1:
            return {'days': [_day(1, 'Explore independent gallery art') ]}
        if day_number == 2:
            if self.honor_ban and ctx.get('avoid_repeat_visits'):
                return {'days': [_day(2, 'Browse creative local makers market') ]}
            return {'days': [_day(2, 'Visit Columbus Museum of Art') ]}
        return {'days': [_day(3, 'Tour Columbus Museum of Art') ]}


async def _run(model):
    return await generate_daily_trip(model,
            request='Plan three days in Columbus for local art and food.',
            destination='Columbus', days=3,
            fields={'days': 3, 'destination': 'Columbus'}, preferences={})


@pytest.mark.asyncio
async def test_targeted_repair_preserves_intended_blueprint_owner_and_other_days():
    model = RepairConflictModel()
    result = await _run(model)
    assert model.attempts == {1: 1, 2: 2, 3: 1}
    assert 'museum' not in result['days'][1]['activities'][0]['description'].lower()
    assert 'Columbus Museum of Art' in result['days'][2]['activities'][0]['description']
    repair = next(c for c in model.contexts if c.get('task') == 'repair_cross_day_conflict')
    assert repair['day_number'] == 2
    assert repair['avoid_repeat_visits'] == ['columbus museum of art']
    assert 'columbus museum of art' not in [a.lower() for a in repair['allocated_days'][0]['anchors']]
    first_ctx = next(c for c in model.contexts if c.get('day_number') == 1)
    assert any(r['day'] == 3 and 'Columbus Museum of Art' in r['anchors']
               for r in first_ctx['reserved_for_future_days'])


@pytest.mark.asyncio
async def test_persistent_named_venue_collision_still_rejects_atomic_trip():
    model = RepairConflictModel(honor_ban=False)
    with pytest.raises(TripValidationError, match='Cross-day consistency failed for Day 2'):
        await _run(model)
    assert model.attempts == {1: 1, 2: 5, 3: 1}


@pytest.mark.asyncio
async def test_distinct_district_experiences_do_not_trigger_unnecessary_regeneration():
    class OsakaModel(RepairConflictModel):
        async def complete_global_blueprint(self, system, prompt, *, day_count, compact=False):
            return {'blueprint': {'destination': 'Osaka', 'title': 'Osaka three days',
                    'summary': 'Explore distinct experiences in Osaka neighborhoods.',
                    'days': [_alloc(1, 'Shinsekai district'), _alloc(2, 'Umeda'),
                             _alloc(3, 'Shinsekai district')]}}

        async def complete_daily_detail(self, system, prompt, *, day_number, compact=False):
            self.attempts[day_number] = self.attempts.get(day_number, 0) + 1
            return {'days': [_day(day_number, {
                1: 'Photograph street architecture in Shinsekai district',
                2: 'Explore art exhibits in Umeda',
                3: 'Taste local kushikatsu dishes in Shinsekai district',
            }[day_number])]}

    model = OsakaModel()
    result = await generate_daily_trip(model, request='Explore three days in Osaka',
        destination='Osaka', days=3, fields={'days': 3, 'destination': 'Osaka'}, preferences={})
    assert len(result['days']) == 3
    assert model.attempts == {1: 1, 2: 1, 3: 1}

@pytest.mark.asyncio
async def test_unresolved_neighborhood_overlap_is_visible_but_not_an_all_or_nothing_failure():
    class PersistentDistrictModel(RepairConflictModel):
        async def complete_global_blueprint(self, system, prompt, *, day_count, compact=False):
            return {'blueprint': {'destination': 'Osaka', 'title': 'Osaka three days',
                    'summary': 'Explore Osaka neighborhoods and authentic local food.',
                    'days': [_alloc(1, 'Shinsekai district'), _alloc(2, 'Umeda'),
                             _alloc(3, 'Shinsekai district')]}}

        async def complete_daily_detail(self, system, prompt, *, day_number, compact=False):
            self.attempts[day_number] = self.attempts.get(day_number, 0) + 1
            return {'days': [_day(day_number,
                     'Explore Shinsekai district' if day_number != 2
                     else 'Tour galleries and cafes in Umeda')]}

    model = PersistentDistrictModel()
    result = await generate_daily_trip(model, request='Visit Osaka over three days',
        destination='Osaka', days=3, fields={'days': 3, 'destination': 'Osaka'},
        preferences={})
    assert len(result['days']) == 3
    assert model.attempts == {1: 1, 2: 1, 3: 2}
    assert any('Planning quality review:' in item
               for item in result.get('predeparture_checks', []))


@pytest.mark.asyncio
async def test_live_quality_report_marks_unresolved_neighborhood_overlap_degraded(monkeypatch):
    from types import SimpleNamespace

    from scripts import evaluate_travel_quality as quality_script

    trip = {'destination': 'Osaka', 'title': 'Osaka three days',
            'days': [_day(n, f'Experience in Osaka neighborhood {n}') for n in (1, 2, 3)],
            'predeparture_checks': [
                'Planning quality review: Day 3 may repeat an area experience in '
                'shinsekai district; compare both days and replace one visit if needed.']}

    async def model_first(*args, **kwargs):
        return SimpleNamespace(task={'operation': 'create'}, mode='model_direct',
                               skill_calls=[], fields={'destination': 'Osaka', 'days': 3}), None

    async def prepare_trip(self, *args):
        return trip, {'action': 'created', 'changed_days': [1, 2, 3]}, 'Trip created.'

    monkeypatch.setattr(quality_script, 'model_first_plan', model_first)
    monkeypatch.setattr(quality_script.Runtime, '_prepare_trip_update', prepare_trip)

    class Model:
        def router_decision_only(self):
            return True

    case = {'id': 'soft_review', 'kind': 'create', 'request': 'Explore Osaka for three days.',
            'destination': 'Osaka', 'days': 3,
            'fields': {'destination': 'Osaka', 'days': 3}}
    result, state = await quality_script.evaluate_case(
        case, Model(), quality_script.DisabledSkillCatalog(), {})
    assert state is not None
    assert result['passed_structure'] and result['task_success']
    assert result['status'] == 'DEGRADED'
    assert result['execution_success'] is False
    assert result['coordination_review_notes']

@pytest.mark.asyncio
async def test_eleven_day_shinsekai_scenario_keeps_all_days_when_visits_are_distinct():
    class ElevenDayModel:
        def __init__(self):
            self.calls = []

        async def complete_global_blueprint(self, system, prompt, *, day_count, compact=False):
            assert day_count == 11
            anchors = {2: 'Shinsekai district', 10: 'Shinsekai district'}
            return {'blueprint': {'destination': 'Japan', 'title': 'Japan regional journey',
                    'summary': 'A logically sequenced journey across Japanese cities.',
                    'days': [{**_alloc(number, anchors.get(number,
                                f'Neighborhood experience {number}')),
                              'city': 'Osaka'} for number in range(1, 12)]}}

        async def complete_daily_detail(self, system, prompt, *, day_number, compact=False):
            self.calls.append((day_number, json.loads(prompt)))
            description = ({2: 'Walk Shinsekai district to photograph the shopfronts',
                            10: 'Try kushikatsu street food in Shinsekai district'}
                           .get(day_number, f'Visit neighborhood market {day_number}'))
            return {'days': [_day(day_number, description)]}

    model = ElevenDayModel()
    result = await generate_daily_trip(model, request='Plan 11 days in Japan',
        destination='Japan', days=11, fields={'destination': 'Japan', 'days': 11},
        preferences={})
    assert len(result['days']) == 11
    assert len(model.calls) == 11
    assert sorted(day for day, _ in model.calls) == list(range(1, 12))
    assert not any(item.startswith('Planning quality review:')
                   for item in result.get('predeparture_checks', []))
