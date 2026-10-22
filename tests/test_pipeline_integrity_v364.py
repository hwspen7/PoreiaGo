"""Regression contracts drawn from observed long-trip pipeline failures.

Do not encode destination-specific route facts in deterministic travel rules.
Those facts require independent place and transit evidence.
"""
from __future__ import annotations

import json

import pytest

from aligo.content_audit import audit_itinerary
from aligo.pipeline_integrity import (internal_reason_issue, schedule_invariants,
                                      stale_repair_metadata, transition_invariants)
from aligo.segmented_planning import generate_segmented_trip
from aligo.traveler_review import review_traveler_quality
from aligo.trip_state import render_itinerary, validate_itinerary


def test_core_schedule_guard_detects_departure_dinner_overlap_without_skill():
    itinerary_day = {'day': 11, 'activities': [
        {'start_time': '18:00', 'duration_minutes': 180},
        {'start_time': '20:30', 'duration_minutes': 90},
    ]}
    messages = schedule_invariants(itinerary_day)
    assert len(messages) == 1
    assert 'overlap' in messages[0]


def test_core_schedule_guard_detects_multiple_nested_overlaps():
    itinerary_day = {'day': 1, 'activities': [
        {'time': '09:00', 'duration_minutes': 240},
        {'time': '10:00', 'duration_minutes': 60},
        {'time': '11:30', 'duration_minutes': 45},
    ]}
    assert len(schedule_invariants(itinerary_day)) == 2


def test_transfer_origin_must_follow_previous_overnight_city():
    previous = {'day': 8, 'overnight_base': 'Kyoto'}
    allocation = {'day': 9, 'city': 'Osaka', 'overnight': 'Osaka'}
    previous_allocation = {'day': 8, 'city': 'Kyoto', 'overnight': 'Kyoto'}
    invalid = {'day': 9, 'activities': [{
        'start_time': '10:00', 'description': 'Visit the cutlery workshops',
        'transport_hint': 'Local train from Osaka Station to Sakai',
    }]}
    problems = transition_invariants(invalid, previous_day=previous,
                                     previous_allocation=previous_allocation,
                                     allocation=allocation)
    assert problems and 'Kyoto' in ' '.join(problems)
    corrected = {'day': 9, 'activities': [{
        'start_time': '09:00',
        'description': 'Check out in Kyoto, travel to Osaka, drop luggage at the hotel.',
        'transport_hint': 'Train from Kyoto to Osaka and local train onward',
    }]}
    assert transition_invariants(corrected, previous_day=previous,
                                  previous_allocation=previous_allocation,
                                  allocation=allocation) == []


def test_no_new_airport_arrival_is_invented_midtrip():
    problems = transition_invariants(
        {'day': 3, 'activities': [{
            'time': '11:30', 'description': 'Arrive at the new city station',
            'transport_hint': 'Express train from KIX',
        }]},
        previous_day={'day': 2, 'overnight_base': 'Osaka'},
        previous_allocation={'day': 2, 'city': 'Osaka', 'overnight': 'Osaka'},
        allocation={'day': 3, 'city': 'Kyoto', 'overnight': 'Kyoto'},
    )
    assert any('fresh airport arrival' in issue for issue in problems)


def test_activity_replacement_cannot_preserve_obsolete_day_copy():
    old = {'day': 10, 'title': 'Market Flavors',
           'logistics': 'Explore the market area before dinner.',
           'activities': [{'description': 'Browse the morning food market.'}]}
    new = {**old, 'activities': [{'description': 'Visit a historic temple.'}]}
    assert stale_repair_metadata(old, new)
    assert not stale_repair_metadata(old, old)
    specific = {'day': 10, 'title': 'Regional History',
                'logistics': 'Visit the Traditional Crafts Museum in the afternoon.',
                'activities': [{'description': 'Visit a riverside shrine.'}]}
    assert stale_repair_metadata(old, specific,
                                 removed_venues=['Traditional Crafts Museum'])


def test_different_rain_sentences_still_count_as_same_named_venue():
    itinerary = {'days': [
        {'day': 1, 'weather_alternative': 'Visit the Kyoto Museum of Crafts and Design.',
         'activities': []},
        {'day': 2, 'weather_alternative': 'Spend more time inside Kyoto Museum of Crafts and Design.',
         'activities': []},
    ]}
    result = review_traveler_quality(itinerary)
    assert any(i['code'] == 'repeated_weather_venue' for i in result['issues'])


def test_renderer_never_silently_removes_a_bad_reason():
    plan = validate_itinerary({
        'destination': 'Columbus', 'title': 'Art day',
        'days': [{'day': 1, 'title': 'Art and food', 'activities': [
            {'description': 'Tour the contemporary art exhibits at the main museum.',
             'time': 'Morning',
             'why_this_fits': 'Aligns with the Art and Culture theme.'},
        ]}],
    })
    text = render_itinerary(plan)
    assert 'Aligns with the Art and Culture theme' not in text
    assert 'Why:' in text
    assert 'contemporary art exhibits' in text
    assert internal_reason_issue(plan['days'][0])


def test_content_timing_dimension_drops_when_real_overlaps_exist():
    plan = {'days': [{'day': 1, 'title': 'Walking day',
                      'area_cluster': 'Downtown',
                      'logistics': 'Take the metro and walk between venues.',
                      'weather_alternative': 'Visit a local indoor history museum.',
                      'activities': [
                          {'description': 'Explore a local museum collection',
                           'time': '09:00', 'duration_minutes': 120},
                          {'description': 'Have lunch in the neighborhood',
                           'time': '11:30', 'duration_minutes': 90},
                          {'description': 'Tour the old architecture district',
                           'time': '15:30', 'duration_minutes': 90}]}]}
    clean = audit_itinerary(plan)
    plan['days'][0]['activities'][1]['time'] = '10:30'
    overlapping = audit_itinerary(plan)
    assert overlapping['dimensions']['pacing_and_timing'] < clean['dimensions']['pacing_and_timing']


def test_unknown_flight_time_is_not_mistaken_for_ready_departure():
    plan = {'days': [{'day': 1, 'overnight_base': 'N/A',
                      'activities': [{'description': 'Transfer to the airport at 18:00',
                                      'time': '18:00', 'duration_minutes': 90}]}]}
    found = review_traveler_quality(plan)
    assert any(i['code'] == 'departure_flight_unspecified' for i in found['issues'])


class _RetryBadWhyModel:
    def __init__(self):
        self.calls = 0
        self.contexts = []

    async def complete_global_blueprint(self, system, prompt, *, day_count, compact=False):
        return {'blueprint': {
            'destination': 'Columbus', 'title': 'Columbus arts',
            'days': [{'day': 1, 'city': 'Columbus', 'area': 'Downtown',
                      'theme': 'Art', 'anchors': ['Local gallery culture'], 'overnight': 'Columbus',
                      'transfer': '', 'day_type': 'sightseeing'}]}}

    async def complete_daily_detail(self, system, prompt, *, day_number, compact=False):
        self.calls += 1
        self.contexts.append(json.loads(prompt))
        reason = ('Matches the Culture and Heritage theme.' if self.calls == 1
                  else 'See the changing contemporary art exhibits and local artists.')
        return {'days': [{'day': 1, 'title': 'Gallery day',
                          'area_cluster': 'Downtown Columbus',
                          'logistics': 'Walk around downtown then return by bus.',
                          'weather_alternative': 'Spend more time at an indoor gallery.',
                          'activities': [
                              {'description': 'Explore a local contemporary art collection',
                               'time': '09:30', 'duration_minutes': 90,
                               'why_this_fits': reason},
                              {'description': 'Visit an architectural heritage exhibit',
                               'time': '12:00', 'duration_minutes': 90,
                               'why_this_fits': 'See how the city developed historically.'},
                              {'description': 'Enjoy the downtown neighborhood streets',
                               'time': '15:00', 'duration_minutes': 90,
                               'why_this_fits': 'Explore local architecture and creative shops.'},
                          ]}]}


@pytest.mark.asyncio
async def test_day_generator_rewrites_internal_why_before_acceptance():
    model = _RetryBadWhyModel()
    result = await generate_segmented_trip(
        model, request='Plan one day in Columbus with art and heritage.',
        destination='Columbus', days=1,
        fields={'destination': 'Columbus', 'days': 1}, preferences={})
    assert model.calls == 2
    assert model.contexts[0]['journey_state']['current_city'] == 'Columbus'
    assert model.contexts[0]['journey_state']['hotel_change'] is False
    assert result['days'][0]['activities'][0]['why_this_fits'].startswith('See the')
