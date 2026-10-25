"""Regressions for shared create/revise/legacy-router/activity-delta commit invariants."""
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest

from planning.activity_revision import merge_activity_patch
from core.config import settings
from storage.memory import MemoryStore
from services.registry import SkillRegistry
from core.runtime import Runtime
from planning.trip_state import TripValidationError, validate_itinerary
from planning.validation_gate import (ItineraryGateError, explicit_revision_intent,
                                    validate_for_commit, unresolved_schedule_fields)


def itinerary(days=1):
    return validate_itinerary({
        'destination': 'Example City', 'title': 'Museum itinerary',
        'days': [{'day': i, 'title': f'Day {i}', 'activities': [
            {'description': 'Visit a national history gallery', 'start_time': '09:00',
             'duration_minutes': 120},
            {'description': 'Explore a historic street', 'start_time': '11:30',
             'duration_minutes': 90},
            {'description': 'Enjoy an evening meal', 'start_time': '18:30',
             'duration_minutes': 90},
        ]} for i in range(1, days + 1)]})


def issue_codes(document, **kwargs):
    with pytest.raises(ItineraryGateError) as err:
        validate_for_commit(document, **kwargs)
    return [i.code for i in err.value.issues]


def test_complete_created_trip_rejects_known_overlap_even_when_schema_is_valid():
    value = itinerary()
    value['days'][0]['activities'][1]['start_time'] = '10:00'
    assert 'SCHEDULE_INVARIANT' in issue_codes(value)


def test_nonadjacent_overlap_cannot_hide_behind_intermediate_activity():
    value = itinerary()
    value['days'][0]['activities'][0]['duration_minutes'] = 300
    assert 'SCHEDULE_INVARIANT' in issue_codes(value)


def test_all_paths_reject_impossible_incoming_transfer():
    value = itinerary()
    value['days'][0]['activities'][1]['transfer_minutes'] = 50
    assert 'SCHEDULE_INVARIANT' in issue_codes(value)


def test_out_of_order_times_rejected_even_with_missing_duration():
    value = itinerary()
    value['days'][0]['activities'][1]['duration_minutes'] = None
    value['days'][0]['activities'][1]['start_time'] = '08:00'
    # Structural validation omits missing durations, but the clock still runs backwards.
    assert 'ACTIVITY_TIME_ORDER' in issue_codes(value)


def test_known_durations_cannot_cross_midnight():
    value = itinerary()
    value['days'][0]['activities'] = [
        {'description': 'Late evening visit', 'start_time': '23:00', 'duration_minutes': 120}]
    assert 'ACTIVITY_EXCEEDS_DAY' in issue_codes(value)


def test_new_timed_stop_requires_complete_window_on_scheduled_day():
    base = itinerary()
    patch = {'day': 1, 'operations': [
        {'action': 'insert', 'position': 2, 'activity': {
            'description': 'Additional stop', 'start_time': '15:00'}}]}
    with pytest.raises(TripValidationError, match='REVISION_NEW_ACTIVITY_MISSING_DURATION'):
        merge_activity_patch(base, patch, day_number=1)
    assert len(base['days'][0]['activities']) == 3


def test_old_conflict_cannot_be_carried_forward_by_unrelated_delta():
    base = itinerary()
    base['days'][0]['activities'][1]['start_time'] = '10:00'
    with pytest.raises(TripValidationError, match='REVISION_ACTIVITY_TIME_CONFLICT'):
        merge_activity_patch(base, {'day': 1, 'operations': [
            {'action': 'insert', 'position': 3, 'activity': {
                'description': 'Late walk', 'start_time': '21:00',
                'duration_minutes': 60}}]}, day_number=1)


def test_typed_revision_intent_requires_specific_topic_not_just_any_change():
    base = itinerary()
    new = deepcopy(base)
    new['days'][0]['activities'].append({
        'description': 'Go shopping', 'start_time': '20:30', 'duration_minutes': 60})
    constraint = explicit_revision_intent('I want to visit more museums.')
    assert constraint['requested_term'] == 'museum'
    assert 'REVISION_TOPIC_REQUIREMENT_NOT_MET' in issue_codes(
        new, previous=base, intent=constraint)


def test_nighttime_constraint_is_checked_against_actual_new_activity():
    base = itinerary()
    new = deepcopy(base)
    new['days'][0]['activities'].append({
        'description': 'An evening garden', 'start_time': '20:30', 'duration_minutes': 60})
    constraint = explicit_revision_intent('Add activities after 8pm')
    assert constraint['not_before_minute'] == 1200
    # An activity starting at 20:30 satisfies a strict 'after 20:00' request.
    assert validate_for_commit(new, previous=base, intent=constraint)
    new['days'][0]['activities'][-1]['start_time'] = '20:00'
    assert 'REVISION_TIME_REQUIREMENT_NOT_MET' in issue_codes(
        new, previous=base, intent=constraint)


def test_comparative_adjective_not_construed_as_an_added_location():
    intent = explicit_revision_intent('Make Day 2 more relaxing.')
    assert 'requested_term' not in intent
    assert 'requires_activity_change' not in intent


def test_arbitrary_https_url_does_not_authorize_verified_evidence():
    plan = itinerary()
    plan['days'][0]['activities'][0].update({
        'evidence_status': 'verified', 'evidence_url': 'https://example.net/unexamined'})
    assert 'UNTRUSTED_VERIFIED_EVIDENCE' in issue_codes(plan)


def test_verified_claim_needs_server_owned_observation():
    plan = itinerary()
    url = 'https://example.net/official'
    plan['days'][0]['activities'][0].update({'evidence_status': 'verified', 'evidence_url': url})
    trusted = {url: {'provider': 'official_api', 'observed_at': '2026-10-20T00:00:00Z',
                     'verified': True}}
    assert validate_for_commit(plan, trusted_sources=trusted)


def test_store_commit_guard_blocks_bad_router_and_preserves_version(tmp_path):
    store = MemoryStore(tmp_path / 'case.sqlite3')
    try:
        prior = itinerary()
        store.save_itinerary('user', 'session', prior)
        corrupted = deepcopy(prior)
        corrupted['days'][0]['activities'][1]['start_time'] = '10:00'
        with pytest.raises(ItineraryGateError):
            store.save_itinerary('user', 'session', corrupted, expected_version=1)
        state = store.active_itinerary('user', 'session')
        assert state['version'] == 1 and state['itinerary'] == prior
        assert len(store.itinerary_versions('user', 'session')) == 1
    finally:
        store.close()


@pytest.mark.asyncio
async def test_legacy_router_cannot_bypass_actual_revision_patch(tmp_path):
    store = MemoryStore(tmp_path / 'legacy.sqlite3')
    prior = itinerary()
    store.save_itinerary('user', 'session', prior)

    class MockModel:
        def __init__(self):
            self.patch_calls = 0
        def configured(self, role):
            return role in {'router', 'reasoning', 'synthesis'}
        def router_decision_only(self):
            return False  # Simulate an old profile that would return full itineraries.
        async def complete(self, role, system, prompt, json_mode=False, **kwargs):
            assert role == 'router'
            corrupted = deepcopy(prior)
            corrupted['days'][0]['activities'][1]['start_time'] = '10:00'
            return {'mode': 'direct', 'operation': 'revise', 'itinerary': corrupted,
                    'answer': '', 'goals': [], 'fields': {}, 'skills': []}
        async def complete_revision_scope(self, *args, **kwargs):
            return {'target_days': [1]}
        async def complete_revision_patch(self, *args, **kwargs):
            self.patch_calls += 1
            return {'day': 1, 'operations': [{'action': 'insert', 'position': 3,
                    'activity': {'description': 'Visit another art gallery',
                                 'start_time': '21:00', 'duration_minutes': 60}}]}

    model = MockModel()
    try:
        runtime = Runtime(store, model, object(),
                          SkillRegistry(Path(__file__).resolve().parents[2] / 'plugins/skills'),
                          replace(settings, agent_mode='model-first'), retrieval=object())
        response = await runtime.execute('user', 'session', 'Add another evening art gallery.')
        assert model.patch_calls == 1
        assert response['active_itinerary']['version'] == 2
        assert response['active_itinerary']['itinerary']['days'][0]['activities'][1] == prior['days'][0]['activities'][1]
    finally:
        store.close()


def test_unresolved_durations_visible_without_inventing_them():
    plan = itinerary()
    plan['days'][0]['activities'][1].pop('duration_minutes')
    assert 'ACTIVITY_DURATION_UNVERIFIED' in [
        issue.code for issue in unresolved_schedule_fields(plan)]
    # Legacy/approximate content may still be saved, but is never counted as
    # confirmed fully timed by the schedule uncertainty report.
    assert validate_for_commit(plan)


def test_persistence_rejects_fabricated_verified_link_without_partial_save(tmp_path):
    store = MemoryStore(tmp_path / 'verified.sqlite3')
    try:
        value = itinerary()
        value['days'][0]['activities'][0].update({
            'evidence_status': 'verified', 'evidence_url': 'https://example.net/fake'})
        with pytest.raises(ItineraryGateError, match='UNTRUSTED_VERIFIED_EVIDENCE'):
            store.save_itinerary('user', 'trip', value)
        assert store.active_itinerary('user', 'trip') is None
    finally:
        store.close()


def test_unrequested_day_remains_unchanged_and_cannot_be_edited():
    prior = itinerary(days=2)
    updated = deepcopy(prior)
    updated['days'][1]['activities'][0]['description'] = 'Changed Day 2'
    constraint = explicit_revision_intent('Change Day 1', target_days=[1])
    assert 'REVISION_OUTSIDE_REQUESTED_DAYS' in issue_codes(
        updated, previous=prior, intent=constraint)


def test_food_break_is_not_required_by_hard_gate():
    plan = itinerary()
    plan['days'][0]['activities'] = plan['days'][0]['activities'][:2]
    assert validate_for_commit(plan)


def test_atomic_store_rejects_silent_trip_restructure(tmp_path):
    store = MemoryStore(tmp_path / 'shape.sqlite3')
    try:
        prior = itinerary()
        store.save_itinerary('u', 's', prior)
        proposal = deepcopy(prior)
        proposal['destination'] = 'Another City'
        with pytest.raises(TripValidationError, match='cannot silently change the destination'):
            store.save_itinerary('u', 's', proposal, expected_version=1)
        proposal = itinerary(days=2)
        with pytest.raises(TripValidationError, match='cannot silently change the trip duration'):
            store.save_itinerary('u', 's', proposal, expected_version=1)
        assert store.active_itinerary('u', 's')['version'] == 1
    finally:
        store.close()


def test_stage_router_never_uses_legacy_full_itinerary_mode(tmp_path, monkeypatch):
    import json
    from intelligence.models import ModelGateway
    models = tmp_path / 'models.json'
    models.write_text(json.dumps({'profiles': [{
        'name': 'legacy-router', 'role': 'router', 'provider': 'openai-compatible',
        'model': 'example', 'base_url': 'http://127.0.0.1:11434/v1',
        'api_key_env': 'POREIAGO_OLLAMA_API_KEY', 'decision_only': False,
        'max_output_tokens': 1024}]}), encoding='utf-8')
    monkeypatch.setenv('POREIAGO_OLLAMA_API_KEY', 'ollama')
    gateway = ModelGateway(replace(settings, models_file=models))
    assert gateway.router_decision_only() is True
