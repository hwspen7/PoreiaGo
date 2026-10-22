"""Baseline-v1 regression tests for source traceability and travel readiness."""
from copy import deepcopy

import pytest

from aligo.itinerary_quality_guard import reconcile_daily_content
from aligo.place_evidence import analyze_evidence_routes, match_place_candidates
from aligo.traveler_review import review_traveler_quality
from aligo.content_audit import audit_itinerary
from aligo.travel_intelligence import TravelSkillOrchestrator


def fixture_itinerary(*, second_time='10:30', mode='Walk from the museum.'):
    return {'days': [{'day': 1, 'title': 'Two independent visits',
                      'activities': [
        {'place_name': 'Sample Museum', 'description': 'Explore Sample Museum collections.',
         'start_time': '09:00', 'duration_minutes': 90},
        {'place_name': 'Sample Gallery', 'description': 'Explore Sample Gallery exhibitions.',
         'start_time': second_time, 'duration_minutes': 60, 'transport_hint': mode},
    ]}]}


def fixture_candidates(*, coordinates=True):
    museum = {'name': 'Sample Museum', 'place_id': 'museum-1', 'area': 'Downtown'}
    gallery = {'name': 'Sample Gallery', 'place_id': 'gallery-2', 'area': 'Riverside'}
    if coordinates:
        museum.update(latitude=40.0, longitude=-83.0)
        gallery.update(latitude=40.0, longitude=-82.98)
    return [{'day': 1, 'city': 'Columbus', 'places': [museum, gallery]}]


def test_candidate_linkage_keeps_provider_identity_and_coordinates():
    plan = fixture_itinerary()
    result = match_place_candidates(plan, fixture_candidates())
    assert result['candidate_count'] == 2
    assert len(result['matches']) == 2
    assert result['matches'][1]['candidate']['place_id'] == 'gallery-2'
    assert result['matches'][1]['candidate']['longitude'] == -82.98
    assert result['matches'][1]['candidate']['city'] == 'Columbus'
    assert result['matches'][1]['candidate']['candidate_day'] == 1
    assert result['matches'][1]['verified'] is False


def test_walk_gap_uses_only_lower_bound_with_coordinates():
    trip = fixture_itinerary()
    result = analyze_evidence_routes(trip, fixture_candidates())
    assert result['assessed_walking_legs'] == 1
    assert len(result['warnings']) == 1
    assert result['warnings'][0]['available_minutes'] == 0
    assert result['warnings'][0]['minimum_minutes_at_6kmh'] > 0
    assert 'not_real_routing' in result['method']
    issues = review_traveler_quality(trip, evidence=fixture_candidates())['issues']
    assert any(row['code'] == 'walking_transfer_infeasible_lower_bound' for row in issues)


def test_walking_route_feasible_is_not_rejected():
    result = analyze_evidence_routes(fixture_itinerary(second_time='11:30'), fixture_candidates())
    assert result['assessed_walking_legs'] == 1
    assert result['warnings'] == []


@pytest.mark.parametrize('evidence,mode', [([], 'Walk from the museum.'),
    (fixture_candidates(coordinates=False), 'Walk from the museum.'),
    (fixture_candidates(), 'Take a taxi.'),
])
def test_incomplete_evidence_and_nonwalking_modes_do_not_claim_route(evidence, mode):
    result = analyze_evidence_routes(fixture_itinerary(mode=mode), evidence)
    assert result['assessed_walking_legs'] == 0
    assert result['warnings'] == []


def test_repeated_rain_museum_from_columbus_actual_report_gets_existing_indoor_option():
    prior = {'day': 1, 'weather_alternative': 'If it rains, the Columbus Museum of Art offers an indoor option.'}
    day = {'day': 3, 'title': 'Statehouse and riverfront',
           'weather_alternative': ('If raining, spend the afternoon inside the Ohio Statehouse '
              'for extended tours or visit the nearby Columbus Museum of Art '
              '(if time permits) for indoor cultural exploration.'),
           'activities': [
                {'description': 'Begin the final day with a visit to the Ohio Statehouse, '
                                'exploring historic architecture.'},
                {'description': 'Walk along the Scioto Mile Waterfront Walk.'}]}
    old = deepcopy(day)
    revised, changes = reconcile_daily_content(day, [prior], request='Visit Columbus')
    assert any('rain venue' in note for note in changes)
    assert 'Ohio Statehouse' in revised['weather_alternative']
    assert 'Columbus Museum of Art' not in revised['weather_alternative']
    assert revised['activities'] == old['activities']
    assert day == old


def test_benchmark_quality_keeps_source_and_content_separate():
    itinerary = fixture_itinerary()
    review = audit_itinerary(itinerary, evidence=fixture_candidates(), request='Explore the city')
    assert review['dimensions']['candidate_traceability'] == 100
    assert review['traveler_review']['route_evidence']['assessed_walking_legs'] == 1
    # This is an observable itinerary warning, never a claim of live route verification.
    assert review['verification_status'] != 'verified'


@pytest.mark.asyncio
async def test_skill_trace_is_evidence_snapshot_not_new_skill():
    controller = TravelSkillOrchestrator(None, None)
    outcome = await controller.day_context(
        {'city': 'Columbus', 'places': [
            {'name': 'Sample Museum', 'type': 'attraction', 'place_id': 'museum-1',
             'latitude': 40.0, 'longitude': -83.0, 'source_url': 'https://example.org/museum'}]},
        {'day': 1, 'city': 'Columbus', 'area': 'Downtown', 'theme': 'Museums',
         'anchors': ['Sample Museum']})
    assert outcome['places'][0]['latitude'] == 40.0
    assert outcome['places'][0]['source_url'] == 'https://example.org/museum'
    trace = next(row for row in controller.trace if row['skill'] == 'day-candidate-evidence')
    assert trace['kind'] == 'candidate_evidence'
    assert trace['candidate_places'][0]['latitude'] == 40.0
    assert trace['candidate_places'][0]['source_url'] == 'https://example.org/museum'


def test_explicit_transfer_at_same_time_without_coordinates_is_not_silently_accepted():
    trip = fixture_itinerary()
    review = review_traveler_quality(trip, evidence=[])
    assert any(row['code'] == 'unbudgeted_transfer' for row in review['issues'])
    assert review['route_evidence']['assessed_walking_legs'] == 0


def test_same_named_venue_does_not_trigger_an_unbudgeted_transfer():
    trip = fixture_itinerary()
    trip['days'][0]['activities'][1]['place_name'] = 'Sample Museum'
    review = review_traveler_quality(trip, evidence=[])
    assert 'unbudgeted_transfer' not in [row['code'] for row in review['issues']]


def test_transport_with_time_gap_is_not_flagged_without_geocoding():
    trip = fixture_itinerary(second_time='11:30')
    review = review_traveler_quality(trip, evidence=[])
    assert 'unbudgeted_transfer' not in [row['code'] for row in review['issues']]


def test_identical_place_name_in_different_cities_respects_day_base_city():
    plan = {'days': [{'day': 1, 'base_city': 'Columbus', 'activities': [
        {'place_name': 'Central Museum', 'description': 'Tour Central Museum.'}]}]}
    evidence = [
        {'city': 'Other City', 'places': [
            {'name': 'Central Museum', 'place_id': 'other-1'}]},
        {'city': 'Columbus', 'places': [
            {'name': 'Central Museum', 'place_id': 'columbus-2'}]},
    ]
    matched = match_place_candidates(plan, evidence)
    assert matched['matches'][0]['candidate']['place_id'] == 'columbus-2'
    assert matched['matches'][0]['verified'] is False


def test_wrong_city_only_does_not_create_a_false_verified_link():
    plan = {'days': [{'day': 1, 'base_city': 'Columbus', 'activities': [
        {'place_name': 'Central Museum', 'description': 'Tour Central Museum.'}]}]}
    result = match_place_candidates(plan, [
        {'city': 'Other City', 'places': [
            {'name': 'Central Museum', 'place_id': 'other-1'}]}])
    assert not result['matches']
    assert len(result['unmatched']) == 1


def test_source_area_diagnostic_does_not_claim_certified_geography():
    trip = {'days': [{'day': 1, 'title': 'Local art', 'area_cluster': 'University District',
                     'base_city': 'Columbus', 'activities': [
                         {'place_name': 'Example Gallery',
                          'description': 'Explore the Example Gallery.'}]}]}
    evidence = [{'city': 'Columbus', 'places': [
        {'name': 'Example Gallery', 'area': 'Downtown'}]}]
    result = review_traveler_quality(trip, evidence=evidence)
    assert any(item['code'] == 'candidate_area_outside_day_cluster'
               and item['severity'] == 'info' for item in result['issues'])
