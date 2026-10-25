"""Regressions derived from the Columbus three-day traveler quality review."""
from __future__ import annotations

from copy import deepcopy

from quality.content_audit import audit_itinerary
from planning.itinerary_quality_guard import (
    day_evidence_issues, reconcile_daily_content,
    reconcile_candidate_locality)
from quality.traveler_review import review_traveler_quality
from planning.segmented_planning import DAY_SYSTEM


def columbus_days():
    return [
        {'day': 1, 'title': 'Downtown Art', 'area_cluster': 'Downtown',
         'weather_alternative': 'Stay inside Columbus Museum of Art if it rains.',
         'activities': [
             {'description': 'Explore Columbus Museum of Art.',
              'place_name': 'Columbus Museum of Art', 'area': 'Downtown'}]},
        {'day': 2, 'title': 'Village and Gardens',
         'area_cluster': 'German Village and Franklin Park',
         'weather_alternative': (
             'If it rains, swap the park for a visit to Columbus Museum of Art '
             'or find a cozy cafe.'),
         'activities': [
             {'description': 'Tour German Village architecture.', 'area': 'German Village'},
             {'description': 'Visit Franklin Park Conservatory botanical exhibits.',
              'place_name': 'Franklin Park Conservatory', 'area': 'Franklin Park'}]},
        {'day': 3, 'title': 'University Walk', 'area_cluster': 'University District',
         'weather_alternative': 'Spend time in a university library if it rains.',
         'activities': [{'description': 'Explore the university campus.'}]},
    ]


def test_repeated_rain_museum_is_replaced_with_same_day_indoor_stop():
    days = columbus_days()
    original = deepcopy(days[1])
    fixed, changes = reconcile_daily_content(
        days[1], days[:1], request='Culture and food in Columbus', final_day=False)
    assert 'Franklin Park Conservatory' in fixed['weather_alternative']
    assert 'Columbus Museum of Art' not in fixed['weather_alternative']
    assert any('rain venue' in item for item in changes)
    assert days[1] == original
    assert fixed['activities'] == original['activities']


def test_missing_dinner_gets_optional_area_based_note_not_invented_business():
    days = columbus_days()
    fixed, changes = reconcile_daily_content(
        days[1], days[:1], request='I want culture and local food')
    assert 'Optional dinner:' in fixed['notes']
    assert 'German Village and Franklin Park' in fixed['notes']
    assert any('dinner' in change for change in changes)
    outcome = review_traveler_quality({'days': [days[0], fixed, days[2]]},
                                      request='Plan history and food in Columbus')
    assert 'no_dinner_option' not in [issue['code'] for issue in outcome['issues']]


def test_no_unsolicited_meal_or_airport_instructions():
    day = columbus_days()[1]
    fixed, _ = reconcile_daily_content(day, [], request='Gardens and architecture')
    assert 'notes' not in fixed
    assert all('airport' not in str(value).lower() for value in fixed.values())


def test_repeating_rain_without_valid_indoor_candidate_does_not_make_up_venue():
    old = columbus_days()[0]
    row = {'day': 2, 'activities': [{'description': 'Walk along the riverfront.'}],
           'weather_alternative': 'Visit Columbus Museum of Art if it rains.'}
    fixed, messages = reconcile_daily_content(row, [old], request='History')
    assert fixed == row
    assert any('verified local alternative' in x for x in messages)


def test_location_conflict_rejected_only_when_provider_states_explicit_neighborhood():
    row = {'day': 3, 'activities': [
        {'description': 'Visit Example Statehouse Museum.',
         'place_name': 'Example Statehouse Museum', 'area': 'University District'}]}
    candidates = {'places': [{'name': 'Example Statehouse Museum',
                              'area': 'Downtown', 'verification_level': 'map_candidate'}]}
    assert day_evidence_issues(row, candidates)
    corrected = deepcopy(row)
    corrected['activities'][0]['area'] = 'Downtown'
    assert not day_evidence_issues(corrected, candidates)


def test_conflicting_provider_neighborhood_recovers_without_fabricating_route():
    day = {'day': 2, 'activities': [
        {'place_name': 'Example Museum', 'area': 'University District',
         'description': 'Tour Example Museum to learn about local architecture.'}]}
    source = {'places': [{'name': 'Example Museum', 'area': 'Downtown'}]}
    result, messages = reconcile_candidate_locality(day, source)
    assert 'area' not in result['activities'][0]
    assert 'Confirm the exact venue location' in result['activities'][0]['verification_needed']
    assert not day_evidence_issues(result, source)
    assert day['activities'][0]['area'] == 'University District'
    assert messages


def test_incomplete_candidates_are_not_a_fictional_place_denylist():
    row = {'day': 2, 'activities': [
        {'place_name': 'Old Donation House', 'area': 'German Village',
         'description': 'Visit Old Donation House.'}]}
    partial = {'places': [{'name': 'Franklin Park Conservatory', 'area': 'Franklin Park'}]}
    assert day_evidence_issues(row, partial) == []
    assert day_evidence_issues(row, {}) == []


def test_verbose_but_unlocatable_activities_cannot_automatically_get_perfect_specificity():
    sample = {'days': [{'day': 1, 'title': 'Generic venues',
              'area_cluster': 'City center',
              'logistics': 'Walk through nearby central streets and take breaks.',
              'weather_alternative': 'If it rains, visit an indoor venue.',
              'activities': [
                  {'description': 'Explore an extraordinary local site where culture and history merge with unique traditions.',
                   'time': 'Morning', 'why_this_fits': 'Learn about the rich heritage of the wider region.'},
                  {'description': 'Visit a wonderful nearby venue with highly engaging community exhibitions and performances.',
                   'time': 'Afternoon', 'why_this_fits': 'Enjoy a peaceful afternoon and interesting things.'}]}]}
    result = audit_itinerary(sample, request='Plan one day')
    assert result['dimensions']['experience_specificity'] < 100


def test_prompts_require_candidate_names_and_true_neighborhoods():
    assert 'exact place_name' in DAY_SYSTEM
    assert 'Do not relocate an attraction' in DAY_SYSTEM
    assert 'do not invent plausible-sounding' in DAY_SYSTEM


def test_airport_score_is_not_central_to_city_only_request():
    plan = {'days': [{'day': 1, 'title': 'Explore',
                     'area_cluster': 'Downtown',
                     'logistics': 'Walk through the streets near the museum.',
                     'weather_alternative': 'Explore the nearby indoor museum if it rains.',
                     'overnight_base': 'N/A',
                     'activities': [{'description': 'Transfer to airport at 18:00',
                                     'start_time': '18:00', 'duration_minutes': 90}]}]}
    a = audit_itinerary(plan, request='Plan a city visit')
    b = audit_itinerary(plan, request='Plan a flight and airport transfer city visit')
    assert a['dimensions']['departure_and_assumptions'] >= b['dimensions']['departure_and_assumptions']


def test_provenance_matches_an_explicit_venue_in_the_description():
    from services.place_evidence import match_place_candidates
    itinerary = {'days': [{'day': 1, 'activities': [
        {'description': 'Explore Franklin Park Conservatory and its displays.'}]}]}
    evidence = [{'places': [{'name': 'Franklin Park Conservatory',
                             'area': 'Franklin Park', 'place_id': 'mock-1'}]}]
    result = match_place_candidates(itinerary, evidence)
    assert result['status'] == 'candidate_matching_only'
    assert len(result['matches']) == 1
    assert result['matches'][0]['candidate']['place_id'] == 'mock-1'


def test_unsupported_named_venue_is_diagnostic_not_proof_of_fiction():
    from services.place_evidence import match_place_candidates
    itinerary = {'days': [{'day': 1, 'activities': [
        {'description': 'Visit an independently named historic house.',
         'place_name': 'Old Donation House'}]}]}
    evidence = [{'places': [{'name': 'Franklin Park Conservatory'}]}]
    result = match_place_candidates(itinerary, evidence)
    assert len(result['unmatched']) == 1
    quality = audit_itinerary(itinerary, request='Visit Columbus', evidence=evidence)
    assert quality['dimensions']['candidate_traceability'] == 0
    assert 'does not establish nonexistence' in ' '.join(quality['findings'])


import json
import pytest
from planning.segmented_planning import generate_segmented_trip


class ConcurrentCityModel:
    def daily_concurrency_limit(self):
        return 3

    async def complete(self, role, system, prompt, json_mode=False):
        payload = json.loads(prompt)
        if 'allocated_days' not in payload:
            return {'blueprint': {'destination': 'Columbus', 'title': 'Columbus neighborhoods',
                'summary': 'Culture, local cuisine and parks.',
                'days': [{'day': i, 'city': 'Columbus', 'area': f'District {i}',
                          'theme': 'Culture and food', 'anchors': [f'Culture stop {i}'],
                          'overnight': 'Columbus', 'transfer': '', 'day_type': 'sightseeing'}
                         for i in range(1, 4)]}}
        number = payload['day_number']
        if number == 1:
            activities = [{'description': 'Enjoy a morning at Columbus Museum of Art.',
                           'time': 'Morning', 'start_time': '09:30', 'duration_minutes': 75},
                          {'description': 'Explore the historic district for lunch.',
                           'time': 'Lunch', 'start_time': '12:30', 'duration_minutes': 60}]
            activities.append({'description': 'Walk along the downtown riverfront promenade.',
                               'time': 'Afternoon', 'start_time': '15:30', 'duration_minutes': 60})
            backup = 'Visit Columbus Museum of Art if it rains.'
        elif number == 2:
            activities = [{'description': 'Explore German Village on a history walk.',
                           'time': 'Morning', 'start_time': '09:30', 'duration_minutes': 75},
                          {'description': 'See Franklin Park Conservatory botanical exhibits.',
                           'place_name': 'Franklin Park Conservatory',
                           'time': 'Afternoon', 'start_time': '13:30', 'duration_minutes': 90}]
            activities.append({'description': 'Enjoy regional lunch and local dishes near German Village.',
                               'time': 'Lunch', 'start_time': '12:00', 'duration_minutes': 60})
            activities.append({'description': 'Explore a scenic neighborhood park in the afternoon.',
                               'time': 'Late Afternoon', 'start_time': '16:00', 'duration_minutes': 60})
            backup = 'Spend a rainy afternoon inside the Columbus Museum of Art.'
        else:
            activities = [{'description': 'Enjoy a neighborhood architecture walk.',
                           'time': 'Morning', 'start_time': '09:30', 'duration_minutes': 60},
                          {'description': 'Explore a culinary market for lunch.',
                           'time': 'Lunch', 'start_time': '12:30', 'duration_minutes': 60}]
            activities.append({'description': 'Walk through a locally recognized public square.',
                               'time': 'Afternoon', 'start_time': '15:30', 'duration_minutes': 60})
            backup = 'Choose a covered venue for rainy conditions.'
        return {'days': [{'day': number, 'title': f'Columbus day {number}',
                          'area_cluster': f'District {number}',
                          'logistics': 'Walk within the same neighborhood as the attractions.',
                          'weather_alternative': backup,
                          'activities': activities}]}


@pytest.mark.asyncio
async def test_parallel_days_receive_post_assembly_weather_and_meal_reconciliation():
    result = await generate_segmented_trip(
        ConcurrentCityModel(), request='Plan three days in Columbus for history and food.',
        destination='Columbus', days=3,
        fields={'destination': 'Columbus', 'days': 3},
        preferences={}, providers=None, registry=None)
    assert len(result['days']) == 3
    second = result['days'][1]
    assert 'Franklin Park Conservatory' in second['weather_alternative']
    assert 'Columbus Museum of Art' not in second['weather_alternative']
    assert 'Optional dinner' in second['notes']


def test_place_skill_preserves_explicit_provider_area_information():
    from services.skill_support import place_record
    source = {'name': 'Example Historical Center',
              'area': 'Downtown', 'neighborhood': 'Statehouse District',
              'latitude': 39.97, 'longitude': -83.00}
    row = place_record(source)
    assert row['area'] == 'Downtown'
    assert row['neighborhood'] == 'Statehouse District'
