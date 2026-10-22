"""Travel-first V3.6 regressions: do not confuse content completeness with truth."""
from copy import deepcopy
from pathlib import Path

import pytest

from aligo.content_audit import audit_itinerary
from aligo.segmented_planning import _cross_day_issues, _named_attraction_visits
from aligo.traveler_review import review_traveler_quality
from aligo.trip_state import TripValidationError, render_itinerary, validate_itinerary
from scripts.evaluate_travel_quality import save_report


def plan():
    return {
        'destination': 'Japan', 'title': 'Regional culture',
        'days': [
            {'day': 1, 'title': 'Culture', 'overnight_base': 'Osaka',
             'weather_alternative': 'Visit the downtown handicrafts museum if it rains.',
             'budget_note': 'Each traveler pays individually.',
             'activities': [
                 {'time': 'Morning', 'description': 'Visit Yasaka Shrine',
                  'why_this_fits': 'The architecture and local rituals make this a distinctive stop.',
                  'cost_estimate': '1,200 JPY per person (unverified)'},
                 {'time': 'Afternoon', 'description': 'Lunch featuring local cuisine, specifically Kaki-no-ha sushi',
                  'why_this_fits': 'Try a regional specialty with a distinct preparation style.',
                  'cost_estimate': '2,000 JPY per person (unverified)'},
             ]},
            {'day': 2, 'title': 'Crafts', 'overnight_base': 'Nara',
             'weather_alternative': 'Visit the downtown handicrafts museum if it rains.',
             'budget_note': 'Costs are for two adults.',
             'activities': [
                 {'time': 'Morning', 'description': 'Take a pottery workshop with a local artisan',
                  'why_this_fits': 'Aligns with the Craft and Tradition theme.',
                  'cost_estimate': '4,000 JPY (unverified) (unverified)'},
                 {'time': 'Lunch', 'description': 'Lunch featuring local cuisine, specifically Kaki-no-ha sushi',
                  'why_this_fits': 'Try the same specialty served in a different restaurant.'},
             ]},
        ],
    }


def test_review_finds_separate_traveler_problems_without_mutation():
    itinerary = plan()
    old = deepcopy(itinerary)
    issues = review_traveler_quality(itinerary)['issues']
    codes = {issue['code'] for issue in issues}
    assert {'meal_time_mismatch', 'internal_planning_language',
            'duplicate_price_caveat', 'repeated_dish',
            'repeated_weather_backup', 'mixed_cost_basis'} <= codes
    assert itinerary == old


def test_quality_score_not_inflated_by_long_template_text():
    generic = plan()
    polished = deepcopy(generic)
    polished['days'][0]['activities'][1]['time'] = 'Lunch'
    polished['days'][1]['activities'][0]['why_this_fits'] = (
        'Practice the techniques of the craft and see how local materials are used.')
    polished['days'][1]['activities'][0]['cost_estimate'] = '4,000 JPY'
    polished['days'][1]['weather_alternative'] = 'Spend the rainy afternoon at a nearby indoor gallery.'
    polished['days'][1]['budget_note'] = 'Costs are per person.'
    polished['days'][1]['activities'][1]['description'] = 'Lunch featuring seasonal vegetables and a local noodle dish'
    assert audit_itinerary(generic)['score'] < audit_itinerary(polished)['score']
    assert audit_itinerary(generic)['traveler_review']['review_required']


def test_render_removes_blueprint_phrases_and_duplicate_uncertainty():
    validated = validate_itinerary(plan())
    output = render_itinerary(validated)
    assert 'Aligns with the Craft and Tradition theme' not in output
    assert '(unverified) (unverified)' not in output
    assert 'Estimated cost: 4,000 JPY (unverified)' in output
    assert 'Yasaka Shrine' in output


def test_explicit_time_and_evidence_metadata_round_trip():
    itinerary = plan()
    activity = itinerary['days'][0]['activities'][0]
    activity.update(activity_type='attraction', place_name='Yasaka Shrine',
                    start_time='09:30', cost_basis='per_person', cost_currency='JPY',
                    evidence_status='pending', evidence_url='https://example.org/official',
                    evidence_title='Venue information')
    validated = validate_itinerary(itinerary)
    saved = validated['days'][0]['activities'][0]
    for name in ('activity_type', 'place_name', 'start_time', 'cost_basis',
                 'cost_currency', 'evidence_status', 'evidence_url', 'evidence_title'):
        assert saved[name] == activity[name]
    assert '- 09:30: Visit Yasaka Shrine' in render_itinerary(validated)


@pytest.mark.parametrize('patch', [
    {'start_time': '25:99'}, {'cost_basis': 'made_up'},
    {'activity_type': 'unsafe'}, {'evidence_url': 'http://example.org'},
    {'evidence_status': 'verified'},
])
def test_untrusted_metadata_fails_closed(patch):
    itinerary = plan()
    itinerary['days'][0]['activities'][0].update(patch)
    with pytest.raises(TripValidationError):
        validate_itinerary(itinerary)


def test_model_cannot_claim_external_verification_by_attaching_a_url():
    itinerary = plan()
    itinerary['days'][0]['activities'][0].update(
        evidence_status='verified', evidence_url='https://example.org/unexamined')
    review = review_traveler_quality(validate_itinerary(itinerary))
    assert review['verified_cost_items'] == 0
    assert review['verification_status'] == 'pending'


def test_named_embedded_attraction_detects_duplicates_without_blueprint_anchors():
    days = [{'day': 1, 'activities': [{'description': 'Afternoon exploration of Yasaka Shrine and surrounding streets'}]},
            {'day': 2, 'activities': [{'description': 'Photograph Yasaka Shrine and the surrounding neighborhood'}]}]
    blueprint = [{'day': 1, 'anchors': []}, {'day': 2, 'anchors': []}]
    issues = _cross_day_issues(days, blueprint, request='Two days of sightseeing')
    assert 2 in issues
    assert 'yasaka shrine' in issues[2][0]


def test_incidental_transport_does_not_count_as_attraction_visit():
    assert _named_attraction_visits('Take a train past Yasaka Shrine') == []
    assert _named_attraction_visits('Walk past Yasaka Shrine on your way to the hotel') == []


def test_full_markdown_report_preserves_late_days(tmp_path: Path):
    itinerary = {'destination': 'Japan', 'title': 'Thirty days', 'days': []}
    for day in range(1, 31):
        itinerary['days'].append({
            'day': day, 'title': f'Culture Day {day}',
            'activities': [{'description': f'Visit a named historical venue on Day {day}, '
                            + ('viewing regional craftsmanship ' * 11)} for _ in range(4)]})
    itinerary = validate_itinerary(itinerary)
    markdown = tmp_path / 'complete.md'
    save_report({'results': [{'model': 'example', 'trial': 1, 'case_id': 'full_30',
                              'status': 'PASS', 'score': 100, 'passed_structure': True,
                              'task_success': True, 'elapsed_seconds': 1.0, 'warnings': [],
                              'role_calls': [], 'itinerary': itinerary}]}, markdown)
    text = markdown.read_text(encoding='utf-8')
    assert len(render_itinerary(itinerary)) > 22000
    assert 'Day 30: Culture Day 30' in text
    assert 'viewing regional craftsmanship' in text[text.index('Day 30:') :]


def test_source_candidates_match_exact_named_place_without_claiming_fact_check():
    from aligo.place_evidence import extract_place_candidates, match_place_candidates
    evidence = [{'name': 'Source wrapper', 'data': {
        'results': [{'name': 'Yasaka Shrine', 'source_url': 'https://example.org/shrine'},
                    {'name': 'Kyoto Craft Museum'}]}}]
    extracted = extract_place_candidates(evidence)
    assert {row['name'] for row in extracted} >= {'Yasaka Shrine', 'Kyoto Craft Museum'}
    itinerary = plan()
    itinerary['days'][0]['activities'][0]['place_name'] = 'Yasaka Shrine'
    itinerary['days'][1]['activities'][0]['place_name'] = 'Unrecognized Studio'
    grounded = match_place_candidates(itinerary, evidence)
    assert grounded['status'] == 'candidate_matching_only'
    assert len(grounded['matches']) == len(grounded['unmatched']) == 1
    assert grounded['unmatched'][0]['name'] == 'Unrecognized Studio'


def test_evidence_absence_is_not_claimed_to_disprove_place():
    from aligo.place_evidence import match_place_candidates
    itinerary = plan()
    itinerary['days'][1]['activities'][0]['place_name'] = 'Small Community Museum'
    grounding = match_place_candidates(itinerary, None)
    assert grounding['status'] == 'no_source_candidates'
    assert grounding['unmatched'] == []


def test_overnight_change_requires_transfer_planning_in_quality_review():
    itinerary = plan()
    review = review_traveler_quality(itinerary)
    assert any(issue['code'] == 'missing_intercity_transfer' for issue in review['issues'])
    itinerary['days'][1]['intercity_transfer'] = 'Morning rail journey with luggage transfer'
    assert not any(issue['code'] == 'missing_intercity_transfer'
                   for issue in review_traveler_quality(itinerary)['issues'])


def test_exact_schedule_overlap_is_reported_without_changing_itinerary():
    itinerary = plan()
    items = itinerary['days'][0]['activities']
    items[0]['start_time'] = '09:30'
    items[0]['duration_minutes'] = 120
    items[1]['start_time'] = '10:45'
    problems = review_traveler_quality(itinerary)['issues']
    assert any(issue['code'] == 'activity_time_overlap' for issue in problems)


def test_untrusted_evidence_sample_does_not_issue_false_verified_costs():
    itinerary = plan()
    itinerary['days'][0]['activities'][0]['place_name'] = 'Yasaka Shrine'
    result = audit_itinerary(itinerary, evidence=[{
        'name': 'Yasaka Shrine', 'source_url': 'https://example.org/source'}])
    assert result['traveler_review']['place_evidence']['matches']
    assert result['verification_status'] == 'pending'
