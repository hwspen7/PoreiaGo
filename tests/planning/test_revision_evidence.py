"""Batch 4: changed-place evidence refresh and cross-day transfer uncertainty."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.config import settings
from storage.memory import MemoryStore
from services.registry import SkillRegistry
from planning.revision_evidence import (
    audit_revision_place_evidence, changed_place_visits,
    cross_day_transfer_uncertainties, _source_metadata,
)
from core.runtime import Runtime
from planning.trip_state import validate_itinerary

ROOT = Path(__file__).resolve().parents[2]


def baseline(days=1):
    return validate_itinerary({'destination': 'City A', 'title': 'City trip',
        'days': [{'day': n, 'title': f'Day {n}', 'base_city': 'City A',
                  'activities': [
                      {'description': 'Old Museum', 'place_name': 'Old Museum',
                       'activity_type': 'attraction', 'start_time': '09:00',
                       'duration_minutes': 60, 'why_this_fits': 'Local history'},
                      {'description': 'City walk', 'start_time': '12:00',
                       'duration_minutes': 60},
                  ]} for n in range(1, days + 1)]})


def changed_plan():
    old = baseline()
    updated = deepcopy(old)
    updated['days'][0]['activities'][0] = {
        'description': 'New Museum', 'place_name': 'New Museum',
        'activity_type': 'attraction', 'start_time': '09:00',
        'duration_minutes': 60, 'evidence_status': 'pending'}
    return old, updated


class FakeProviders:
    def __init__(self, *, online=True, matches=1, fail=False, observed_at=None):
        self.online = online
        self.matches = matches
        self.fail = fail
        self.calls = []
        self.observed_at = observed_at or datetime.now(timezone.utc).isoformat()

    def available(self, kind):
        return self.online

    async def request(self, capability, **kwargs):
        self.calls.append((capability, kwargs))
        if self.fail:
            from services.providers import ProviderUnavailable
            raise ProviderUnavailable('offline provider')
        if capability == 'geocode':
            return SimpleNamespace(data={'results': [{'latitude': 35., 'longitude': 120.}]},
                                   evidence=[])
        assert capability == 'places'
        return SimpleNamespace(data={'results': [
            {'name': 'New Museum', 'place_id': str(i)} for i in range(self.matches)]},
            evidence=[SimpleNamespace(provider='test-map', observed_at=self.observed_at)])


def test_new_or_renamed_venue_requires_review_without_mutating_previous():
    old, updated = changed_plan()
    updates = changed_place_visits(old, updated)
    assert len(updates) == 1
    assert updates[0]['name'] == 'New Museum'
    assert changed_place_visits(old, old) == []


def test_unchanged_venue_reordered_or_explanation_edit_not_refetched():
    old = baseline()
    changed = deepcopy(old)
    changed['days'][0]['activities'][0]['why_this_fits'] = 'Another reason'
    assert changed_place_visits(old, changed) == []


def test_same_venue_rescheduled_requires_new_review():
    old = baseline()
    changed = deepcopy(old)
    changed['days'][0]['activities'][0]['start_time'] = '10:00'
    assert len(changed_place_visits(old, changed)) == 1


@pytest.mark.asyncio
async def test_online_exact_map_match_only_candidate_not_verified():
    old, new = changed_plan()
    provider = FakeProviders()
    before = deepcopy(new)
    review = await audit_revision_place_evidence(old, new, provider)
    assert review['status'] == 'review_complete'
    assert review['reviewed_places'] == 1
    assert review['places'][0]['status'] == 'mapped_candidate'
    assert review['places'][0]['freshness'] == 'recent_observation'
    assert review['places'][0]['provider'] == 'test-map'
    assert review['places'][0].get('verified') is None
    assert provider.calls[0][0] == 'geocode'
    assert provider.calls[1][0] == 'places'
    assert new == before


@pytest.mark.asyncio
async def test_offline_never_calls_network_and_reports_unverified():
    old, new = changed_plan()
    provider = FakeProviders(online=False)
    review = await audit_revision_place_evidence(old, new, provider)
    assert review['status'] == 'provider_unavailable'
    assert review['places'][0]['status'] == 'unverified_offline'
    assert provider.calls == []


@pytest.mark.asyncio
async def test_ambiguous_name_or_absent_candidate_never_verified():
    old, new = changed_plan()
    assert (await audit_revision_place_evidence(old, new, FakeProviders(matches=2)))['places'][0]['status'] == 'ambiguous_name_match'
    assert (await audit_revision_place_evidence(old, new, FakeProviders(matches=0)))['places'][0]['status'] == 'no_candidate_match'


@pytest.mark.asyncio
async def test_provider_failure_is_reported_without_rejecting_plan():
    old, new = changed_plan()
    result = await audit_revision_place_evidence(old, new, FakeProviders(fail=True))
    assert result['status'] == 'partial'
    assert result['places'][0]['status'] == 'provider_error'


def test_source_timestamps_are_not_assumed_fresh():
    old = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    stale = _source_metadata([SimpleNamespace(provider='map', observed_at=old)],
                             freshness_seconds=3600)
    assert stale['freshness'] == 'stale_observation'
    assert _source_metadata([SimpleNamespace(provider='map', observed_at='unknown')],
                             freshness_seconds=3600)['freshness'] == 'unknown'
    future = (datetime.now(timezone.utc) + timedelta(days=5)).isoformat()
    assert _source_metadata([SimpleNamespace(provider='map', observed_at=future)],
                            freshness_seconds=3600)['freshness'] == 'unknown'


def test_cross_day_handoffs_do_not_invent_transfer_times():
    value = baseline(3)
    value['days'][1]['base_city'] = 'City B'
    value['days'][2]['base_city'] = 'City B'
    issues = cross_day_transfer_uncertainties(value)
    assert len(issues) == 1
    assert issues[0]['status'] == 'transfer_not_specified'
    assert 'minutes' not in issues[0]
    value['days'][1]['intercity_transfer'] = 'Travel by rail after checking service'
    assert cross_day_transfer_uncertainties(value)[0]['status'] == 'transfer_claim_unverified'


def test_multi_day_unknown_city_is_not_guessed_from_destination():
    old = baseline(2)
    new = deepcopy(old)
    new['days'][1].pop('base_city')
    new['days'][1]['activities'][0]['place_name'] = 'New Museum'
    changes = changed_place_visits(old, new)
    assert changes[0]['city'] == ''


class RevisionModel:
    def configured(self, role):
        return role == 'router'

    def router_decision_only(self):
        return True

    async def complete(self, role, system, prompt, json_mode=False, **kwargs):
        return {'operation': 'revise', 'mode': 'direct', 'itinerary': None,
                'answer': '', 'fields': {}, 'goals': [], 'skills': []}

    async def complete_revision_scope(self, *args, **kwargs):
        return {'target_days': [1]}

    async def complete_revision_patch(self, *args, **kwargs):
        return {'day': 1, 'operations': [{'action': 'replace', 'position': 1,
            'activity': {'description': 'New Museum', 'place_name': 'New Museum',
                         'start_time': '09:00', 'duration_minutes': 60}}]}


@pytest.mark.asyncio
async def test_runtime_revision_returns_review_without_influencing_commit(tmp_path):
    store = MemoryStore(tmp_path / 'review.sqlite3')
    try:
        store.save_itinerary('u', 's', baseline())
        provider = FakeProviders(matches=1)
        runtime = Runtime(store, RevisionModel(), provider,
            SkillRegistry(ROOT / 'plugins/skills'),
            replace(settings, agent_mode='model-first'), retrieval=object())
        output = await runtime.execute('u', 's', 'Replace old museum with a new venue.')
        assert output['active_itinerary']['version'] == 2
        assert output['revision_evidence_audit']['places'][0]['status'] == 'mapped_candidate'
        assert output['active_itinerary']['itinerary']['days'][0]['activities'][0].get('evidence_status') != 'verified'
        assert len(store.itinerary_versions('u', 's')) == 2
    finally:
        store.close()
