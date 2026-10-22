"""Offline coverage for richer travel content and trustworthy evaluation results."""
from copy import deepcopy
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from aligo.config import settings
from aligo.model_agent import DECISION_ONLY_SYSTEM, ITINERARY_REPAIR_SYSTEM
from aligo.models import ModelGateway
from aligo.quality_evaluation import assess_discussion, assess_itinerary
from aligo.runtime import Runtime
from scripts.evaluate_travel_quality import (DisabledSkillCatalog, evaluate_case,
                                             save_report)


def trip():
    return {'destination': 'Example Region', 'title': 'A Balanced Local Journey',
            'preferences': {'pace': 'moderate'}, 'days': [
                {'day': n, 'title': f'Day {n}: Neighborhood Exploration',
                 'activities': [
                     {'description': 'Visit a traditional cultural district with a local guide.'},
                     {'description': 'Explore the regional food market and local cuisine.'}]}
                for n in range(1, 6)]}


def test_content_prompts_require_local_distinctiveness_and_truthful_grounding():
    assert 'destination-specific named sites' in ITINERARY_REPAIR_SYSTEM
    assert 'unnecessary backtracking' in ITINERARY_REPAIR_SYSTEM
    assert 'notes field for each day' in ITINERARY_REPAIR_SYSTEM
    assert 'Do not fabricate' in ITINERARY_REPAIR_SYSTEM
    assert 'RETURN ONLY' in ITINERARY_REPAIR_SYSTEM
    assert 'current_active_itinerary' in DECISION_ONLY_SYSTEM


def test_schema_can_pass_without_receiving_a_perfect_content_score():
    row = assess_itinerary({'kind': 'create', 'days': 5,
                            'destination': 'Example Region'}, trip())
    assert row['passed_structure'] is True
    assert row['checks']['distinct_activities'] is False
    assert row['checks']['practical_notes'] is False
    assert row['score'] < 100
    assert row['content_quality_warnings']


def test_unsupported_accessibility_claim_fails_task_even_when_json_is_valid():
    candidate = deepcopy(trip())
    candidate['summary'] = 'This route is fully accessible and no walking required.'
    row = assess_itinerary({'kind': 'create', 'days': 5,
                            'request': 'Plan for a traveler with limited mobility'}, candidate)
    # With an explicit mobility request, unsupported guarantees require review.
    assert row['checks']['no_unsupported_accessibility'] is False
    assert row['passed_structure'] is True
    assert row['task_success'] is False


def test_context_denial_cannot_count_as_completed_summary():
    frame = SimpleNamespace(task={'operation': 'discuss'})
    answer = ('No tengo acceso a un itinerario previo en esta conversación. '
              'Por favor proporcióname la información y entonces puedo resumir '
              'los cinco días y explicar todos los cambios del día tres.')
    result = assess_discussion({'kind': 'discuss', 'language': 'es'}, frame, answer,
                                previous=trip(), revision_delta={'changed_days': [3]})
    assert result['task_success'] is False
    assert result['score'] == 0
    assert not result['checks']['context_available']


class NoModels:
    def router_decision_only(self):
        raise AssertionError('Blocked dependents must never invoke the model')


@pytest.mark.asyncio
async def test_failed_parent_blocks_summary_without_model_calls():
    case = {'id': 'dependent_summary', 'kind': 'discuss',
            'depends_on': 'failed_revision', 'request': 'Summarize the revised trip'}
    result, state = await evaluate_case(case, NoModels(), DisabledSkillCatalog(), {})
    assert state is None
    assert result['status'] == 'BLOCKED'
    assert result['task_success'] is False
    assert result['role_calls'] == []


def test_read_only_evaluation_has_no_skill_catalog():
    catalog = DisabledSkillCatalog()
    assert catalog.specs == {}
    assert catalog.get('any-skill') is None


def test_report_has_three_outcome_states_and_human_review(tmp_path):
    report = tmp_path / 'quality.json'
    results = [{'model': 'local', 'trial': 1, 'case_id': name,
                'status': status, 'score': score, 'warnings': [],
                'passed_structure': status == 'PASS', 'elapsed_seconds': 2,
                'task_success': status == 'PASS', 'role_calls': []}
               for name, status, score in [('success', 'PASS', 92),
                                           ('failure', 'FAIL', 0),
                                           ('dependent', 'BLOCKED', 0)]]
    save_report({'results': results}, report)
    text = report.with_suffix('.md').read_text(encoding='utf-8')
    assert '| PASS |' in text
    assert '| FAIL |' in text
    assert '| BLOCKED |' in text
    assert '1 PASS, 1 FAIL, 1 BLOCKED' in text
    assert 'Manual rubric' in text


@pytest.mark.asyncio
async def test_local_gateway_records_token_usage_without_credentials(monkeypatch, tmp_path):
    cfg = tmp_path / 'models.json'
    cfg.write_text(json.dumps({'profiles': [
        {'name': 'ollama-router', 'provider': 'openai-compatible',
         'role': 'router', 'model': 'fake-model',
         'base_url': 'http://127.0.0.1:11434/v1',
         'api_key_env': 'ALIGO_OLLAMA_API_KEY'}]}), encoding='utf-8')
    monkeypatch.setenv('ALIGO_OLLAMA_API_KEY', 'dummy-local-key')
    monkeypatch.setitem(sys.modules, 'openai', SimpleNamespace(AsyncOpenAI=lambda **kwargs: None))
    gateway = ModelGateway(SimpleNamespace(models_file=cfg, request_timeout=10))

    class FakeCompletions:
        async def create(self, **kwargs):
            return SimpleNamespace(
                choices=[SimpleNamespace(finish_reason='stop',
                                         message=SimpleNamespace(content='{"mode":"direct"}'))],
                usage=SimpleNamespace(prompt_tokens=120, completion_tokens=42,
                                      total_tokens=162))

    gateway._clients['ollama-router'] = SimpleNamespace(
        chat=SimpleNamespace(completions=FakeCompletions()))
    assert await gateway.complete('router', 'system', 'prompt', json_mode=True) == {
        'mode': 'direct'}
    assert gateway.last_completion['completion_tokens'] == 42
    assert gateway.last_completion['finish_reason'] == 'stop'
    assert 'dummy-local-key' not in json.dumps(gateway.last_completion)


def test_runtime_detects_only_genuine_active_trip_denial():
    assert Runtime._needs_active_trip_answer('No tengo acceso a un itinerario previo.')
    assert Runtime._needs_active_trip_answer('')
    assert not Runtime._needs_active_trip_answer('Resumen del plan actualizado en cinco días.')


def test_single_day_revision_never_changes_other_saved_days():
    baseline = trip()
    proposed = deepcopy(baseline)
    proposed['days'][2]['title'] = 'Day 3: New Cultural Experiences'
    proposed['days'][0]['title'] = 'Unrequested changed title'
    proposed['days'][4]['activities'] = [{'description': 'Replace unrelated day activity'}]
    safe = Runtime._preserve_unrequested_days(
        proposed, baseline, 'Make Day 3 more exciting, keep other days unchanged.')
    assert safe['days'][2]['title'] == 'Day 3: New Cultural Experiences'
    assert safe['days'][0] == baseline['days'][0]
    assert safe['days'][4] == baseline['days'][4]
    assert proposed['days'][0] != baseline['days'][0]


def test_broad_revision_keeps_model_control_of_requested_scope():
    baseline = trip()
    changed = deepcopy(baseline)
    changed['days'][0]['title'] = 'Updated morning'
    changed['days'][3]['title'] = 'Updated afternoon'
    assert Runtime._preserve_unrequested_days(
        changed, baseline, 'Make the whole trip more relaxed.') == changed


@pytest.mark.asyncio
async def test_summary_of_changes_reads_previous_saved_version(tmp_path):
    from dataclasses import replace
    from aligo.memory import MemoryStore
    from aligo.registry import SkillRegistry

    store = MemoryStore(tmp_path / 'test_only.sqlite3')
    original = trip()
    updated = deepcopy(original)
    updated['days'][2]['title'] = 'Day 3: Nara Local Experiences'
    store.save_itinerary('traveler', 'session', original, expected_version=0)
    store.save_itinerary('traveler', 'session', updated, expected_version=1)

    class FakeModels:
        def __init__(self):
            self.discussion_payload = None

        def configured(self, role):
            return role in {'router', 'reasoning', 'synthesis'}

        def router_decision_only(self):
            return True

        async def complete(self, role, system, prompt, json_mode=False):
            if role == 'router':
                return {'mode': 'direct', 'operation': 'discuss',
                        'answer': 'No tengo acceso a un itinerario previo.',
                        'fields': {}, 'skills': [], 'goals': []}
            if role == 'synthesis':
                self.discussion_payload = json.loads(prompt)
                return ('En el día 3, la nueva versión cambia el recorrido para '
                        'incluir experiencias locales en Nara. El resto del '
                        'itinerario permanece igual, sin cambios en el presupuesto.')
            raise AssertionError('No other model request should be needed.')

    try:
        models = FakeModels()
        runtime = Runtime(store, models, None,
                          SkillRegistry(settings.extensions_dir),
                          replace(settings, agent_mode='model-first'), retrieval=object())
        outcome = await runtime.execute('traveler', 'session',
                                        '¿Qué cambió en el día 3?')
        assert 'Nara' in outcome['answer']
        assert outcome['active_itinerary']['version'] == 2
        assert models.discussion_payload['previous_version'] == original
        assert models.discussion_payload['changes']['changed_days'] == [3]
        assert len(store.itinerary_versions('traveler', 'session')) == 2
        assert store.itinerary_version_document('traveler', 'session', 1) == original
        assert store.itinerary_version_document('another_user', 'session', 1) is None
    finally:
        store.close()
