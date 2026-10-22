"""Verify nine-skill discovery actually informs day-level planning decisions."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from aligo.contracts import Evidence
from aligo.providers import ProviderResult
from aligo.registry import SkillRegistry
from aligo.segmented_planning import generate_segmented_trip
from aligo.travel_intelligence import TravelSkillOrchestrator

ROOT = Path(__file__).resolve().parents[1]


def registry():
    return SkillRegistry(ROOT / 'plugins' / 'skills')


class PlacesProvider:
    def __init__(self):
        self.calls = []

    def available(self, capability):
        return capability in {'geocode', 'places'}

    async def request(self, capability, **kwargs):
        self.calls.append((capability, kwargs))
        if capability == 'geocode':
            value = {'results': [{'name': 'Osaka', 'country': 'Japan',
                                   'latitude': 34.67, 'longitude': 135.5}]}
        elif capability == 'places':
            if kwargs.get('kind') == 'tourism':
                value = {'results': [
                    {'name': 'Day One Museum', 'category': 'history', 'osm_id': 101,
                     'latitude': 34.670, 'longitude': 135.500},
                    {'name': 'Day Two Garden', 'category': 'garden', 'osm_id': 102,
                     'latitude': 34.680, 'longitude': 135.510},
                    {'name': 'Artisan Studio', 'category': 'craft', 'osm_id': 103,
                     'latitude': 34.675, 'longitude': 135.505},
                    {'name': 'River View', 'category': 'park', 'osm_id': 104,
                     'latitude': 34.685, 'longitude': 135.515},
                ]}
            else:
                value = {'results': [
                    {'name': 'Regional Noodles', 'category': 'restaurant', 'osm_id': 105,
                     'latitude': 34.68, 'longitude': 135.51}]}
        else:
            raise AssertionError(f'Unexpected provider request: {capability}')
        return ProviderResult(value, [Evidence(provider='test-map')])


@pytest.mark.asyncio
async def test_daily_selection_reserves_future_anchors_and_excludes_completed_visits():
    p = PlacesProvider()
    controller = TravelSkillOrchestrator(registry(), p)
    candidates = await controller.candidate_context(
        [{'city': 'Osaka', 'theme': 'History and food'},
         {'city': 'Osaka', 'theme': 'Craft and garden'}],
        request='History, crafts, gardens and food in Osaka', fields={'destination': 'Japan'})
    assert len([name for name, _ in p.calls if name == 'geocode']) == 1
    city = candidates['osaka']
    first = await controller.day_context(city,
        {'day': 1, 'city': 'Osaka', 'area': 'Downtown', 'theme': 'History',
         'anchors': ['Day One Museum']},
        future_anchors=['Day Two Garden'])
    first_names = {row['name'] for row in first['places']}
    assert 'Day One Museum' in first_names
    assert 'Day Two Garden' not in first_names
    second = await controller.day_context(city,
        {'day': 2, 'city': 'Osaka', 'area': 'Garden district', 'theme': 'Craft',
         'anchors': ['Day Two Garden']},
        previous_days=[{'activities': [{'description': 'Visit Day One Museum in the morning.'}]}])
    second_names = {row['name'] for row in second['places']}
    assert 'Day One Museum' not in second_names
    assert 'Day Two Garden' in second_names
    assert any(row.get('type') == 'restaurant' for row in second['places'])
    assert second['proximity_hint']['suggested_order']
    assert 'walking time' in second['proximity_hint']['caveat']
    assert any(item['skill'] == 'multi-stop-route-optimizer' for item in controller.trace)
    assert len([name for name, _ in p.calls if name == 'places']) == 2


@pytest.mark.asyncio
async def test_daily_context_without_coordinates_does_not_fabricate_route():
    controller = TravelSkillOrchestrator(registry(), None)
    data = await controller.day_context(
        {'city': 'Osaka', 'places': [{'name': 'Temple', 'type': 'attraction'},
                                    {'name': 'Museum', 'type': 'attraction'}]},
        {'day': 1, 'city': 'Osaka', 'area': 'Center', 'theme': 'Heritage', 'anchors': []})
    assert 'proximity_hint' not in data
    assert {item['name'] for item in data['places']} == {'Temple', 'Museum'}


@pytest.mark.asyncio
async def test_daily_context_cannot_promote_provider_candidates_to_verified_places():
    controller = TravelSkillOrchestrator(registry(), None)
    data = await controller.day_context(
        {'city': 'Osaka', 'places': [{'name': 'Craft Workshop', 'type': 'attraction',
            'verification_level': 'map_candidate', 'matching_score': 8}]},
        {'day': 2, 'city': 'Osaka', 'area': 'Center', 'theme': 'Craft', 'anchors': []})
    assert data['places'][0]['verification_level'] == 'map_candidate'
    assert 'unverified' in data['note'].lower()
    assert not any('bookable' in key for key in data['places'][0])


class TwoDayModel:
    def __init__(self):
        self.prompts = []

    async def complete(self, role, system, prompt, json_mode=False):
        payload = json.loads(prompt)
        self.prompts.append(payload)
        if 'allocated_days' not in payload:
            return {'blueprint': {'destination': 'Osaka', 'title': 'Two days in Osaka',
                'summary': 'History and gardens', 'days': [
                    {'day': 1, 'city': 'Osaka', 'area': 'Downtown', 'theme': 'History',
                     'anchors': ['Day One Museum'], 'overnight': 'Osaka',
                     'transfer': '', 'day_type': 'sightseeing'},
                    {'day': 2, 'city': 'Osaka', 'area': 'Garden district', 'theme': 'Gardens',
                     'anchors': ['Day Two Garden'], 'overnight': 'Osaka',
                     'transfer': '', 'day_type': 'sightseeing'}]}}
        number = payload['day_number']
        if number == 1:
            descriptions = [
                'Visit Day One Museum to discover the local history.',
                'Try regional cuisine for lunch in Downtown.',
                'Explore the riverside architecture and outdoor exhibits.']
        else:
            descriptions = [
                'Visit Day Two Garden for seasonal landscapes.',
                'Enjoy a garden-side lunch in the district.',
                'Join an artisan pottery demonstration in the neighborhood.']
        return {'days': [{'day': number, 'title': f'Osaka discoveries day {number}',
            'area_cluster': 'Downtown' if number == 1 else 'Garden district',
            'logistics': 'Group nearby attractions to minimize unnecessary travel.',
            'weather_alternative': 'Visit an indoor history gallery if it rains.',
            'activities': [
                {'description': description, 'time': time,
                 'start_time': start, 'duration_minutes': 75}
                for description, time, start in zip(descriptions,
                    ['Morning', 'Lunch', 'Afternoon'], ['09:30', '12:15', '14:30'])]}]}


@pytest.mark.asyncio
async def test_actual_daily_planner_receives_distinct_day_specific_candidates():
    model = TwoDayModel()
    trace = []
    trip = await generate_segmented_trip(
        model, request='Plan two days in Osaka with history gardens and food.',
        destination='Osaka', days=2,
        fields={'destination': 'Osaka', 'days': 2, 'country': 'Japan'},
        preferences={}, providers=PlacesProvider(), registry=registry(), skill_trace=trace)
    assert len(trip['days']) == 2
    daily = {prompt['day_number']: prompt for prompt in model.prompts
             if 'allocated_days' in prompt and 'day_number' in prompt}
    assert {1, 2} <= set(daily)
    first = {row['name'] for row in daily[1]['specialist_skill_candidates']['places']}
    second = {row['name'] for row in daily[2]['specialist_skill_candidates']['places']}
    assert 'Day Two Garden' not in first
    assert 'Day One Museum' not in second
    assert 'Day Two Garden' in second
    assert daily[1]['specialist_skill_candidates']['proximity_hint']['suggested_order']
    assert any(row['skill'] == 'multi-stop-route-optimizer' for row in trace)

@pytest.mark.asyncio
async def test_day_ranking_uses_neighborhood_evidence_over_generic_name_match():
    controller = TravelSkillOrchestrator(registry(), None)
    result = await controller.day_context(
        {'city': 'Osaka', 'places': [
            {'name': 'Town Art Center', 'address': 'North Ward, Osaka',
             'type': 'attraction', 'matching_score': 1},
            {'name': 'East Town Gallery', 'address': 'Namba, Osaka',
             'type': 'attraction', 'matching_score': 1}]},
        {'day': 1, 'city': 'Osaka', 'area': 'Namba',
         'theme': 'Art walk', 'anchors': []})
    assert result['places'][0]['name'] == 'East Town Gallery'
    assert result['places'][0]['address'] == 'Namba, Osaka'


@pytest.mark.asyncio
async def test_prior_visit_detection_is_not_partial_word_matching():
    controller = TravelSkillOrchestrator(registry(), None)
    result = await controller.day_context(
        {'city': 'Osaka', 'places': [
            {'name': 'Garden Hall', 'type': 'attraction'},
            {'name': 'Craft Museum', 'type': 'attraction'}]},
        {'day': 2, 'city': 'Osaka', 'area': 'Old Town', 'theme': 'Arts', 'anchors': []},
        previous_days=[{'activities': [
            {'description': 'Visit the Garden Hallway exhibition.'},
            {'description': 'See Craft Museum on a guided tour.'}]}])
    assert [row['name'] for row in result['places']] == ['Garden Hall']


@pytest.mark.asyncio
async def test_non_english_request_can_use_structured_cultural_preference():
    controller = TravelSkillOrchestrator(registry(), PlacesProvider())
    result = await controller.candidate_context(
        [{'city': 'Osaka', 'theme': 'A day out'}], request='Travel request',
        fields={'destination': 'Japan', 'cultural_interests': ['artisan experience']})
    assert result['osaka']['experiences']
    assert any(row['skill'] == 'cultural-experience-search' for row in controller.trace)
