"""Read-only local-LLM travel quality evaluation with auditable case outputs.

Model-only runs disable Skills and providers; integrated runs may call Skills
and explicitly configured external travel providers. Only Ollama loopback
LLM profiles are enabled, even when cloud keys are configured.
"""
from __future__ import annotations

import argparse
import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import time
from urllib.error import URLError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.config import settings
from quality.benchmark_manifest import evaluation_manifest
from intelligence.model_agent import model_first_plan
from intelligence.models import ModelGateway, ModelUnavailable, _is_loopback_ollama
from quality.quality_evaluation import assess_discussion, assess_itinerary, compare_scores
from services.registry import SkillRegistry
from services.providers import ProviderGateway
from core.runtime import Runtime, extract_fields, safe_model_issue
from planning.trip_state import TripValidationError, render_itinerary


class DisabledSkillCatalog:
    """Disable Skill selection during isolated model-quality evaluation."""
    specs = {}

    @staticmethod
    def get(name):
        return None


def model_stage_label(entry: dict) -> str:
    """Identify blueprint and daily calls without changing role bookkeeping."""
    if entry.get('phase') == 'blueprint':
        return f"{entry['role']}[Blueprint]"
    if entry.get('phase') == 'activity_repair':
        return f"{entry['role']}[Day {entry['day']}: Activity Repair]"
    if 'day' in entry:
        return f"{entry['role']}[Day {entry['day']}]"
    return entry['role']


class TimedGateway:
    """Collect call durations without changing inference arguments."""
    def __init__(self, gateway):
        self.gateway = gateway
        self.role_calls: list[dict] = []

    def router_decision_only(self):
        return self.gateway.router_decision_only()

    def daily_planning_enabled(self):
        method = getattr(self.gateway, "daily_planning_enabled", None)
        return bool(method()) if callable(method) else False

    def daily_concurrency_limit(self):
        method = getattr(self.gateway, "daily_concurrency_limit", None)
        return method() if callable(method) else 1

    def _usage(self):
        getter = getattr(self.gateway, 'completion_metadata', None)
        if callable(getter):
            return deepcopy(getter())
        return deepcopy(getattr(self.gateway, 'last_completion', None))

    async def complete_global_blueprint(self, system, prompt, *, day_count, compact=False):
        """Forward constrained route generation without losing call telemetry."""
        start = time.perf_counter()
        try:
            method = getattr(self.gateway, 'complete_global_blueprint', None)
            if callable(method):
                response = await method(system, prompt, day_count=day_count,
                                        compact=compact)
            else:
                response = await self.gateway.complete(
                    'reasoning', system, prompt, json_mode=True)
            self.role_calls.append({'role': 'reasoning', 'phase': 'blueprint',
                                    'compact': compact, 'days': day_count,
                                    'seconds': round(time.perf_counter() - start, 2),
                                    'status': 'ok', 'prompt_characters': len(prompt),
                                    'usage': self._usage()})
            return response
        except Exception as exc:
            self.role_calls.append({'role': 'reasoning', 'phase': 'blueprint',
                                    'compact': compact, 'days': day_count,
                                    'seconds': round(time.perf_counter() - start, 2),
                                    'status': 'failed', 'prompt_characters': len(prompt),
                                    'issue': (str(exc)[:500] if isinstance(
                                        exc, (ModelUnavailable, TripValidationError))
                                        else type(exc).__name__),
                                    'usage': self._usage()})
            raise

    async def complete_activity_repair(self, system, prompt, *, day_number):
        """Keep activity-only repair and its token usage visible in live diagnostics."""
        start = time.perf_counter()
        try:
            method = getattr(self.gateway, 'complete_activity_repair', None)
            if callable(method):
                response = await method(system, prompt, day_number=day_number)
            else:
                # Old adapters can still return a single activity JSON object.
                response = await self.gateway.complete(
                    'reasoning', system, prompt, json_mode=True)
            self.role_calls.append({'role': 'reasoning', 'phase': 'activity_repair',
                                    'day': day_number,
                                    'seconds': round(time.perf_counter() - start, 2),
                                    'status': 'ok', 'prompt_characters': len(prompt),
                                    'usage': self._usage()})
            return response
        except Exception as exc:
            self.role_calls.append({'role': 'reasoning', 'phase': 'activity_repair',
                                    'day': day_number,
                                    'seconds': round(time.perf_counter() - start, 2),
                                    'status': 'failed', 'prompt_characters': len(prompt),
                                    'issue': (str(exc)[:500] if isinstance(
                                        exc, (ModelUnavailable, TripValidationError))
                                        else type(exc).__name__),
                                    'usage': self._usage()})
            raise

    async def complete_daily_detail(self, system, prompt, *, day_number, compact=False):
        """Preserve bounded daily JSON calls through the diagnostic wrapper."""
        start = time.perf_counter()
        try:
            method = getattr(self.gateway, 'complete_daily_detail', None)
            if callable(method):
                response = await method(system, prompt, day_number=day_number,
                                        compact=compact)
            else:
                response = await self.gateway.complete(
                    'reasoning', system, prompt, json_mode=True)
            self.role_calls.append({'role': 'reasoning', 'day': day_number,
                                    'compact': compact,
                                    'seconds': round(time.perf_counter() - start, 2),
                                    'status': 'ok', 'prompt_characters': len(prompt),
                                    'usage': self._usage()})
            return response
        except Exception as exc:
            self.role_calls.append({'role': 'reasoning', 'day': day_number,
                                    'compact': compact,
                                    'seconds': round(time.perf_counter() - start, 2),
                                    'status': 'failed', 'prompt_characters': len(prompt),
                                    'issue': (str(exc)[:500] if isinstance(
                                        exc, (ModelUnavailable, TripValidationError))
                                        else type(exc).__name__),
                                    'usage': self._usage()})
            raise

    async def complete(self, role, system, prompt, json_mode=False):
        start = time.perf_counter()
        try:
            response = await self.gateway.complete(role, system, prompt, json_mode=json_mode)
            self.role_calls.append({'role': role, 'seconds': round(time.perf_counter() - start, 2),
                                    'status': 'ok', 'prompt_characters': len(prompt),
                                    'usage': self._usage()})
            return response
        except Exception:
            self.role_calls.append({'role': role, 'seconds': round(time.perf_counter() - start, 2),
                                    'status': 'failed', 'prompt_characters': len(prompt),
                                    'issue': (str(sys.exc_info()[1])[:500] if isinstance(
                                        sys.exc_info()[1], (ModelUnavailable, TripValidationError))
                                        else type(sys.exc_info()[1]).__name__),
                                    'usage': self._usage()})
            raise

SUITE = ROOT / 'benchmarks' / 'travel_quality.json'
DEFAULT_REPORT_DIR = ROOT.parent / 'PoreiaGo-Quality-Reports'


def read_cases(path: Path) -> list[dict]:
    payload = json.loads(path.read_text(encoding='utf-8'))
    cases = payload.get('cases')
    if not isinstance(cases, list) or not cases:
        raise ValueError('The evaluation suite must contain a nonempty cases list.')
    seen = set()
    for case in cases:
        if not isinstance(case, dict) or not isinstance(case.get('id'), str):
            raise ValueError('Each evaluation case needs a string id.')
        if case['id'] in seen or case.get('kind') not in {'create', 'revise', 'discuss'}:
            raise ValueError('Duplicate case or unknown operation.')
        if not isinstance(case.get('request'), str) or not case['request'].strip():
            raise ValueError('Each case needs a nonempty request.')
        parent = case.get('depends_on')
        if parent is not None and parent not in seen:
            raise ValueError('Dependent evaluation cases must follow their parent.')
        seen.add(case['id'])
    return cases


def selected_cases(cases: list[dict], *, quick: bool = False,
                   ids: set[str] | None = None) -> list[dict]:
    by_id = {case['id']: case for case in cases}
    active = ({case['id'] for case in cases if case.get('quick')}
              if quick else set(by_id)) if ids is None else set(ids)
    unknown = active - set(by_id)
    if unknown:
        raise ValueError('Unknown cases: ' + ', '.join(sorted(unknown)))
    pending = list(active)
    while pending:
        current = by_id[pending.pop()]
        parent = current.get('depends_on')
        if parent and parent not in active:
            active.add(parent)
            pending.append(parent)
    return [case for case in cases if case['id'] in active]


def require_local_gateway(gateway: ModelGateway) -> ModelGateway:
    """Fail closed rather than unintentionally spending cloud API quota."""
    local = [p for p in gateway.profiles
             if p.get('name') in {'ollama-router', 'ollama-reasoning', 'ollama-synthesis'}
             and p.get('provider') == 'openai-compatible'
             and p.get('base_url', '').rstrip('/') == 'http://127.0.0.1:11434/v1'
             and p.get('api_key_env') == 'POREIAGO_OLLAMA_API_KEY']
    if {p.get('role') for p in local} != {'router', 'reasoning', 'synthesis'}:
        raise ValueError('Expected local Ollama router, reasoning and synthesis profiles. '
                         'Run python -m models.configure_local_llm first.')
    router = next(p for p in local if p['role'] == 'router')
    if router.get('decision_only') is not True or router.get('reasoning_effort') != 'none':
        raise ValueError('The installed Router profile is stale. '
                         'Run python -m models.configure_local_llm.')
    gateway.profiles = local
    return gateway


def installed_models() -> set[str]:
    try:
        with urlopen('http://127.0.0.1:11434/api/tags', timeout=6) as response:
            tags = json.loads(response.read().decode('utf-8'))
    except (OSError, ValueError, URLError) as exc:
        raise ValueError('Ollama is unavailable on localhost:11434. '
                         'Start the local Ollama server first.') from exc
    return {row.get('name', '') for row in tags.get('models', [])}


async def evaluate_case(case: dict, models: ModelGateway, registry: SkillRegistry,
                        saved: dict[str, dict], *, providers=None,
                        skill_mode: str = 'model_only') -> tuple[dict, dict | None]:
    """Execute the real two-stage planner without opening a trip store."""
    start = time.perf_counter()
    models = TimedGateway(models)
    parent_id = case.get('depends_on')
    parent = saved.get(parent_id)
    parent_trip = deepcopy(parent['itinerary']) if parent and parent.get('itinerary') else None
    prior_history = deepcopy(parent['history']) if parent else []
    fields = extract_fields(case['request'])
    if parent_trip:
        # Mirror the live Runtime's preservation of an existing trip's context.
        fields.setdefault('destination', parent_trip['destination'])
        fields.setdefault('days', len(parent_trip['days']))
    fields.update(case.get('fields', {}))
    preferences = dict(parent_trip.get('preferences', {})) if parent_trip else {}
    result: dict = {'case_id': case['id'], 'kind': case['kind'],
                    'expected_operation': case['kind'], 'request': case['request'],
                    'fields': fields, 'score': 0, 'passed_structure': False,
                    'warnings': [], 'elapsed_seconds': None,
                    'task_success': False, 'status': 'FAIL'}
    if parent_id and parent is None:
        result.update(status='BLOCKED', error_code='DEPENDENCY_NOT_SUCCESSFUL',
                      warnings=[f'Parent case {parent_id} did not successfully complete.'],
                      elapsed_seconds=0.0, role_calls=[])
        return result, None
    current_trip = None
    answer = None
    runtime = Runtime(None, models, providers, registry, settings, retrieval=object())
    result['skill_mode'] = skill_mode
    try:
        frame, direct_answer = await model_first_plan(
            case['request'], prior_history, fields, preferences, models,
            registry, settings.max_skill_calls, active_trip=parent_trip)
        result['actual_operation'] = frame.task.get('operation')
        result['planner_mode'] = frame.mode
        result['skills_requested'] = [call['name'] for call in frame.skill_calls]
        if frame.task.get('operation') != case['kind']:
            result['warnings'] = ['Router operation did not match the requested task.']
            return result, None
        if case['kind'] in {'create', 'revise'}:
            current_trip, delta, answer = await runtime._prepare_trip_update(
                case['request'], prior_history,
                {'itinerary': parent_trip} if parent_trip else None,
                '', frame, direct_answer, [], preferences, frame.fields)
            # Reuse actual selected Skill candidates, not imagined search results.
            # An incomplete source sample cannot prove a venue nonexistent.
            venue_evidence = [
                {'day': row.get('day'), 'city': row.get('city'),
                 'places': row.get('candidate_places', [])}
                for row in runtime.last_travel_skill_trace
                if row.get('skill') == 'day-candidate-evidence'
            ]
            quality = assess_itinerary(case, current_trip, previous=parent_trip,
                                       delta=delta, evidence=venue_evidence)
            result.update(quality)
            result['itinerary'] = current_trip
            result['delta'] = delta
        else:
            answer = direct_answer
            if parent_trip and Runtime._needs_active_trip_answer(answer):
                answer = await runtime._complete_active_trip_discussion(
                    case['request'], parent_trip, parent.get('delta') or {},
                    output_language=case.get('language', 'auto'),
                    prior_itinerary=parent.get('prior_itinerary'))
            quality = assess_discussion(case, frame, answer, previous=parent_trip,
                                        revision_delta=parent.get('delta') if parent else None)
            result.update(quality)
        if current_trip and 'traveler_quality_review' in result:
            review = result['traveler_quality_review']
            quality_score = result.get('content_quality_score', 0)
            result['quality_status'] = (
                'REVIEW_REQUIRED' if review.get('review_required') or quality_score < 85
                else 'PROVISIONAL')
            result['verification_status'] = review.get('verification_status', 'not_assessed')
        result['answer'] = answer
        result['passed_structure'] = bool(quality['passed_structure'])
        result['task_success'] = bool(quality.get('task_success', False))
    except (ModelUnavailable, TripValidationError, ValueError,
            TypeError, KeyError, asyncio.TimeoutError) as exc:
        if isinstance(exc, TripValidationError):
            result['error_code'] = 'ITINERARY_VALIDATION_FAILED'
            result['warnings'].append(str(exc)[:500])
            # A hard failure must stay FAIL, but validated earlier days can
            # remain visible for diagnosis instead of vanishing from reports.
            if getattr(exc, 'partial_days', None):
                result['partial_days'] = deepcopy(exc.partial_days)
                result['failed_day'] = getattr(exc, 'failed_day', None)
                result['warnings'].append(
                    'The partial day draft is diagnostic only: incomplete, '
                    'not fully verified and not saved as a completed trip.')
            if getattr(exc, 'rejected_day', None):
                result['rejected_day_draft'] = deepcopy(exc.rejected_day)
                result['failed_day'] = getattr(exc, 'failed_day', None)
        else:
            result['error_code'] = safe_model_issue(exc)
            result['warnings'].append(result['error_code'])
    finally:
        result['elapsed_seconds'] = round(time.perf_counter() - start, 2)
        result['role_calls'] = deepcopy(models.role_calls)
        result['skill_calls_trace'] = deepcopy(runtime.last_travel_skill_trace)
    result['failed_stages'] = [entry['role'] for entry in result['role_calls']
                               if entry['status'] != 'ok']
    # A complete itinerary with a flagged repeated neighborhood may be usable,
    # but it must never receive an unqualified PASS in live evaluation.
    review_notes = [note for note in (current_trip or {}).get('predeparture_checks', [])
                    if note.startswith('Planning quality review:')]
    result['coordination_review_notes'] = review_notes
    result['warnings'].extend(review_notes)
    quality_issues = result.get('traveler_quality_review', {}).get('issues', [])
    significant_quality_defects = (
        sum(issue.get('severity') == 'error' for issue in quality_issues) > 0 or
        sum(issue.get('severity') == 'warning' for issue in quality_issues) >= 3)
    if significant_quality_defects:
        result['warnings'].append(
            'Traveler quality review found multiple substantive defects; '
            'task completion does not establish travel readiness.')
    result['execution_success'] = bool(result['task_success'] and
                                       not result['failed_stages'] and
                                       not review_notes and not significant_quality_defects)
    result['status'] = ('DEGRADED' if result['task_success'] and
                        (result['failed_stages'] or review_notes or significant_quality_defects)
                        else 'PASS' if result['task_success'] else 'FAIL')
    if result['failed_stages']:
        result['warnings'].append('Fallback used after failed model stages: ' +
                                  ', '.join(result['failed_stages']))
    if not result['task_success']:
        return result, None
    output_history = prior_history + [
        {'role': 'user', 'content': case['request']},
        {'role': 'assistant', 'content': answer or
         (render_itinerary(current_trip) if current_trip else '')}]
    return result, {'itinerary': current_trip or parent_trip,
                    'history': output_history, 'delta': result.get('delta'),
                    'prior_itinerary': parent_trip}


async def run_models(cases: list[dict], names: list[str], *, repeats: int = 1,
                     fail_fast: bool = False, with_skills: bool = False,
                     mode: str | None = None, online: bool = False) -> dict:
    if mode not in (None, 'model', 'integrated'):
        raise ValueError('mode must be model or integrated')
    if mode is not None:
        with_skills = mode == 'integrated'
    if online and not with_skills:
        raise ValueError('Online discovery requires integrated mode.')
    # Keep the original model-only benchmark unchanged and provide an explicit
    # end-to-end Skill mode. A mode comparison is not a controlled experiment
    # unless the same model settings, prompts, network state and seed are used.
    registry = (SkillRegistry(settings.extensions_dir) if with_skills
                else DisabledSkillCatalog())
    # Explicit --online permits provider calls for this benchmark only. The
    # regular application configuration is left unchanged.
    provider_settings = replace(settings, allow_online=True) if online else settings
    providers = ProviderGateway(provider_settings) if with_skills else None
    skill_mode = 'skill_orchestrated' if with_skills else 'model_only' 
    records: list[dict] = []
    # Disable all nonlocal profiles even when a cloud API key is configured.
    prior_key = os.environ.get('POREIAGO_OLLAMA_API_KEY')
    os.environ['POREIAGO_OLLAMA_API_KEY'] = 'ollama'
    available = installed_models()
    for name in names:
        if name not in available:
            raise ValueError(f'Model {name!r} is not installed in Ollama. '
                             'Check ollama list or download it first.')
    original = os.environ.get('POREIAGO_LOCAL_MODEL')
    try:
        for name in names:
            os.environ['POREIAGO_LOCAL_MODEL'] = name
            gateway = require_local_gateway(ModelGateway(settings))
            if callable(getattr(gateway, 'selected_profiles', None)) and gateway.selected_profiles() is not None:
                raise ValueError('Legacy --models evaluation cannot use POREIAGO_MODEL_PRESET. '
                                 'Use --preset instead or unset POREIAGO_MODEL_PRESET.')
            for trial in range(1, repeats + 1):
                saved: dict[str, dict] = {}
                for index, case in enumerate(cases, 1):
                    print(f'[{name}] trial={trial}/{repeats} {index}/{len(cases)} '
                          f'{case["id"]}: running', flush=True)
                    row, state = await evaluate_case(case, gateway, registry, saved,
                                                      providers=providers,
                                                      skill_mode=skill_mode)
                    row['model'] = name
                    row['trial'] = trial
                    records.append(row)
                    if state is not None:
                        saved[case['id']] = state
                    print(f'[{name}] {case["id"]}: {row["status"]} '
                          f'score={row["score"]}/100 elapsed={row["elapsed_seconds"]}s', flush=True)
                    if row['status'] != 'PASS':
                        details = '; '.join(str(item) for item in row.get('warnings', []))
                        if row.get('error_code'):
                            details = str(row['error_code']) + (': ' + details if details else '')
                        if details:
                            print(f'  Diagnostic: {details[:1200]}', flush=True)
                        stages = ', '.join(
                            f"{model_stage_label(entry)}={entry['status']}({entry['seconds']}s)"
                            for entry in row.get('role_calls', []))
                        if stages:
                            print('  Stages: ' + stages, flush=True)
                    if fail_fast and row['status'] != 'PASS':
                        break
                if fail_fast and records and records[-1]['status'] != 'PASS':
                    break
            if fail_fast and records and records[-1]['status'] != 'PASS':
                break
    finally:
        if original is None:
            os.environ.pop('POREIAGO_LOCAL_MODEL', None)
        else:
            os.environ['POREIAGO_LOCAL_MODEL'] = original
        if prior_key is None:
            os.environ.pop('POREIAGO_OLLAMA_API_KEY', None)
        else:
            os.environ['POREIAGO_OLLAMA_API_KEY'] = prior_key
    return {'suite': 'travel_quality_v1', 'timestamp_utc': datetime.now(timezone.utc).isoformat(),
            'note': 'Heuristic scores are not a validated proxy for reasoning quality. '
                    'Manual review and identical baseline scenarios are required.',
            'models': names, 'cases': len(cases), 'repeats': repeats,
            'evaluation_mode': skill_mode, 'results': records,
            'comparison': compare_scores(records)}



async def run_model_preset(cases: list[dict], preset: str, *, repeats: int = 1,
                           fail_fast: bool = False, mode: str = 'model',
                           online: bool = False,
                           allow_remote_models: bool = False) -> dict:
    """Evaluate a fixed, explicitly selected stage map without silent fallbacks.

    The legacy --models benchmark remains Ollama-only. A preset can use cloud
    profiles only after an explicit --allow-remote-models opt-in.
    """
    if mode not in {'model', 'integrated'}:
        raise ValueError('mode must be model or integrated')
    if online and mode != 'integrated':
        raise ValueError('Online discovery requires integrated mode.')
    previous = os.environ.get('POREIAGO_OLLAMA_API_KEY')
    os.environ.setdefault('POREIAGO_OLLAMA_API_KEY', 'ollama')
    try:
        gateway = ModelGateway(settings, preset=preset)
        stage_map = gateway.selected_profiles()
        assert stage_map is not None
        external = [stage for stage, spec in stage_map.items()
                    if spec['endpoint'] == 'remote']
        if external and not allow_remote_models:
            raise ValueError('Preset includes remote model stages: ' + ', '.join(external) +
                             '. Add --allow-remote-models to authorize potential API charges.')
        local_models = {spec['model'] for stage, spec in stage_map.items()
                        if stage not in external}
        for stage, profile in gateway._stage_profiles.items():
            if not gateway._configured(profile):
                raise ValueError(f'Missing API key environment variable for preset stage {stage}: '
                                 f'{profile.get("api_key_env", "unknown")}')
        if local_models:
            installed = installed_models()
            missing = local_models - installed
            if missing:
                raise ValueError('Preset models are not installed in Ollama: ' +
                                 ', '.join(sorted(missing)))
        registry = (SkillRegistry(settings.extensions_dir) if mode == 'integrated'
                    else DisabledSkillCatalog())
        provider_settings = replace(settings, allow_online=True) if online else settings
        providers = ProviderGateway(provider_settings) if mode == 'integrated' else None
        skill_mode = 'skill_orchestrated' if mode == 'integrated' else 'model_only'
        records = []
        for trial in range(1, repeats + 1):
            saved = {}
            for index, case in enumerate(cases, 1):
                print(f'[{preset}] trial={trial}/{repeats} {index}/{len(cases)} '
                      f'{case["id"]}: running', flush=True)
                row, state = await evaluate_case(case, gateway, registry, saved,
                                                 providers=providers, skill_mode=skill_mode)
                row['model'] = preset
                row['trial'] = trial
                records.append(row)
                if state is not None:
                    saved[case['id']] = state
                print(f'[{preset}] {case["id"]}: {row["status"]} '
                      f'score={row["score"]}/100 elapsed={row["elapsed_seconds"]}s', flush=True)
                if fail_fast and row['status'] != 'PASS':
                    break
            if fail_fast and records and records[-1]['status'] != 'PASS':
                break
        return {'suite': 'travel_quality_v1',
                'timestamp_utc': datetime.now(timezone.utc).isoformat(),
                'note': 'Heuristic scores require manual review and repeated trials.',
                'models': [preset], 'cases': len(cases), 'repeats': repeats,
                'evaluation_mode': skill_mode, 'results': records,
                'comparison': compare_scores(records),
                'stage_models': stage_map,
                'model_preset': preset}
    finally:
        if previous is None:
            os.environ.pop('POREIAGO_OLLAMA_API_KEY', None)
        else:
            os.environ['POREIAGO_OLLAMA_API_KEY'] = previous

def save_report(data: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    table = ['# PoreiaGo Travel Quality Review', '',
             'A valid structure is not sufficient for task-level success.',
             'Automatic scores are diagnostic signals, not a model-quality verdict.',
             'Review actual itinerary details in the companion JSON file.', '',
             '| Model | Trial | Case | Outcome | Structure | Task checks | Content proxy | Seconds | Warnings |',
             '| --- | ---: | --- | --- | --- | ---: | ---: | ---: | --- |']
    for row in data['results']:
        warnings = '; '.join(row['warnings']).replace('|', '/')[:170]
        table.append(
            f"| {row['model']} | {row.get('trial', 1)} | {row['case_id']} | "
            f"{row.get('status', 'FAIL')} | "
            f"{'PASS' if row['passed_structure'] else 'FAIL'} | "
            f"{row['score']} | {row.get('content_quality_score', 'N/A')} | {row['elapsed_seconds']} | {warnings} |")
    table.extend(['', '## Failure and fallback diagnostics', ''])
    for row in data['results']:
        failures = [entry for entry in row.get('role_calls', [])
                    if entry.get('status') != 'ok']
        if row.get('status') in {'PASS'} and not failures:
            continue
        table.append(f"### {row['model']} / {row['case_id']} / trial {row.get('trial', 1)}")
        table.append(f"- Outcome: {row.get('status', 'FAIL')}; task succeeded: {row.get('task_success', False)}")
        if row.get('error_code'):
            table.append(f"- Error code: {row['error_code']}")
        for warning in row.get('warnings', []):
            table.append('- Warning: ' + str(warning).replace('\n', ' ')[:1200])
        for entry in failures:
            label = model_stage_label(entry)
            table.append(f"- Failed {label}: {entry.get('issue', 'unknown')} "
                         f"(seconds: {entry.get('seconds')}, usage: {entry.get('usage')})")
        table.append('')
    counts = {}
    for row in data['results']:
        counters = counts.setdefault(row['model'], {'PASS': 0, 'DEGRADED': 0, 'FAIL': 0, 'BLOCKED': 0})
        counters[row.get('status', 'FAIL')] += 1
    table.extend(['', '## Task-level outcomes', ''])
    for name, result in counts.items():
        table.append(f'- {name}: {result["PASS"]} PASS, {result["FAIL"]} FAIL, '
                     f'{result["BLOCKED"]} BLOCKED' +
                     (f', {result["DEGRADED"]} DEGRADED' if result['DEGRADED'] else ''))
    table.extend(['', '## Content intelligence audit', '',
                  'Content proxy is not a 1–10 human quality rating. '
                  'Detailed dimensions require manual factual and geographic review.',
                  'Task PASS does not mean traveler-quality approval or source verification.', ''])
    for row in data['results']:
        if 'content_quality_dimensions' not in row:
            continue
        table.append(f"### {row['model']} / {row['case_id']} / trial {row.get('trial', 1)}")
        table.append(f"Content proxy: {row.get('content_quality_score')}/100.")
        table.append(f"Traveler quality: {row.get('quality_status', 'NOT_ASSESSED')}.")
        table.append(f"Fact verification: {row.get('verification_status', 'not_assessed')}.")
        for name, value in row['content_quality_dimensions'].items():
            if value is not None:
                table.append(f'- {name.replace("_", " ")}: {value}/100')
        for issue in row.get('content_quality_findings', []):
            table.append(f'- Review: {issue}')
        benchmark = row.get('benchmark_evidence')
        if benchmark:
            table.append('Benchmark evidence: ' + ', '.join(
                f'{key}={value}' for key, value in benchmark.items()))
        for issue in row.get('traveler_quality_review', {}).get('issues', []):
            if issue.get('severity') in ('warning', 'error'):
                table.append(f"- {issue['severity'].upper()} "
                             f"[{issue['code']}]: {issue['message']}")
        table.append('')
    table.extend(['', '## Generated itineraries and answers', ''])
    for row in data['results']:
        table.append(f"### {row['model']} — trial {row.get('trial', 1)} — {row['case_id']}")
        table.append('')
        if row.get('itinerary'):
            table.append(render_itinerary(row['itinerary']))
        elif row.get('answer'):
            table.append(str(row['answer'])[:22000])
        elif row.get('partial_days'):
            table.append('**Incomplete diagnostic draft — NOT a validated itinerary.**')
            table.append(f"Generation failed at Day {row.get('failed_day', 'unknown')}.")
            for draft in row['partial_days']:
                table.append(f"#### Day {draft.get('day')}: {draft.get('title', 'Untitled')}")
                for activity in draft.get('activities') or []:
                    if isinstance(activity, dict):
                        label = activity.get('start_time') or activity.get('time') or ''
                        description = activity.get('description') or ''
                        table.append(f"- {label}: {description}".strip())
                if draft.get('logistics'):
                    table.append('Logistics: ' + str(draft['logistics'])[:400])
                table.append('')
        else:
            table.append('No usable itinerary or answer was returned.')
        if row.get('rejected_day_draft'):
            rejected = row['rejected_day_draft']
            table.append('')
            table.append('**Rejected day for debugging ONLY — do not travel using these times.**')
            table.append(f"#### Day {rejected.get('day')}: {rejected.get('title', 'Untitled')}")
            for activity in rejected.get('activities') or []:
                if isinstance(activity, dict):
                    time_label = activity.get('start_time') or activity.get('time') or ''
                    description = activity.get('description') or ''
                    duration = activity.get('duration_minutes')
                    table.append(f'- {time_label}: {description} (duration: {duration} min)')
        table.append('')
        table.append('Role timings: ' + ', '.join(
            f"{model_stage_label(entry)}={entry['seconds']}s "
            f"({entry['status']}); usage={entry.get('usage')}"
            for entry in row.get('role_calls', [])))
        table.append(f"Skill evaluation mode: {row.get('skill_mode', 'not_recorded')}.")
        trace = row.get('skill_calls_trace') or []
        skill_entries = [item for item in trace if item.get('skill') != 'day-candidate-evidence']
        evidence_entries = [item for item in trace if item.get('skill') == 'day-candidate-evidence']
        table.append(f"Specialist Skill calls recorded: {len(skill_entries)}.")
        table.append(f"Candidate-evidence snapshots: {len(evidence_entries)} (not Skill calls).")
        for item in skill_entries[:45]:
            table.append(f"- Skill: {item.get('skill')} "
                         f"({item.get('status', 'unknown')}) "
                         f"city={item.get('city', 'not_applicable')} "
                         f"candidates={item.get('candidates', 'n/a')} "
                         f"conflicts={item.get('conflicts', 'n/a')}")
        table.append('')
    table.extend(['', '## Manual rubric', '',
                  'Independently score each itinerary from 1 to 5 for:',
                  '- Geographic coherence and unnecessary backtracking',
                  '- Realistic pacing and sensible transfer days',
                  '- Originality and destination-specific recommendations',
                  '- Faithful handling of budget, accessibility, and traveler preferences',
                  '- Accuracy and transparency around unverified live information',
                  '- Natural language and retention of conversation context',
                  '- Arrival and departure assumptions, including conditional alternatives',
                  '- Booking actions, prices and accessibility claims backed by evidence',
                  '- Weather resilience, backup choices, capacity and fatigue',
                  '- Seasonal closures, unusual holidays and local cultural considerations',
                  '- Distinctive experiences versus stereotyped or over-touristed selections', '',
                  'Compare identical cases, and investigate every hard failure.',
                  'Do not infer quality parity from JSON/schema compliance alone.', ''])
    path.with_suffix('.md').write_text('\n'.join(table), encoding='utf-8')


def main() -> None:
    parser = argparse.ArgumentParser(description='Evaluate local PoreiaGo models without modifying trip state.')
    parser.add_argument('--live', action='store_true',
                        help='Required: authorize local inference. LLM calls remain local; '
                             '--with-skills may use configured external travel data providers.')
    parser.add_argument('--quick', action='store_true', help='Run four focused quality cases.')
    parser.add_argument('--repeats', type=int, default=1, help='Repeat the independent scenario chain.')
    parser.add_argument('--fail-fast', action='store_true',
                        help='Stop at the first failed case to avoid costly repeated inference.')
    parser.add_argument('--mode', choices=['model', 'integrated'], default=None,
                        help='Model-only benchmark or integrated travel Skills pipeline.')
    parser.add_argument('--online', action='store_true',
                        help='Allow external provider discovery only in integrated mode.')
    parser.add_argument('--with-skills', action='store_true',
                        help='Enable the installed Skill registry and configured travel providers. '
                             'Online lookups follow POREIAGO_ALLOW_ONLINE and may make external API requests.')
    parser.add_argument('--models', nargs='+', default=None,
                        help='Installed Ollama models to evaluate serially.')
    parser.add_argument('--preset', help='Named model-stage preset from config/model_presets.json.')
    parser.add_argument('--allow-remote-models', action='store_true',
                        help='Explicitly authorize cloud inference and potential API charges with --preset.')
    parser.add_argument('--case', action='append', dest='case_ids',
                        help='Run one named case and its dependencies; repeat to select more.')
    parser.add_argument('--suite', type=Path, default=SUITE)
    parser.add_argument('--output', type=Path, help='JSON report destination (default: timestamped).')
    parser.add_argument('--list', action='store_true', help='List cases without model calls.')
    args = parser.parse_args()
    try:
        cases = read_cases(args.suite)
        chosen = selected_cases(cases, quick=args.quick,
                                ids=set(args.case_ids) if args.case_ids else None)
        if args.list:
            for row in chosen:
                print(f"{row['id']} ({row['kind']})")
            return
        if not args.live:
            parser.error('Add --live to authorize calls to your local Ollama server.')
        if not 1 <= args.repeats <= 20:
            parser.error('--repeats must be between 1 and 20.')
        if args.preset:
            if args.models is not None:
                parser.error('Use --preset or --models, not both.')
            report = asyncio.run(run_model_preset(
                chosen, args.preset, repeats=args.repeats, fail_fast=args.fail_fast,
                mode=args.mode or ('integrated' if args.with_skills else 'model'),
                online=args.online, allow_remote_models=args.allow_remote_models))
            gateway_for_manifest = ModelGateway(settings, preset=args.preset)
        else:
            if args.allow_remote_models:
                parser.error('--allow-remote-models requires --preset.')
            report = asyncio.run(run_models(chosen, args.models or ['gemma4:12b-it-qat'],
                                            repeats=args.repeats, fail_fast=args.fail_fast,
                                            with_skills=args.with_skills, mode=args.mode,
                                            online=args.online))
            gateway_for_manifest = None
        report['benchmark_manifest'] = evaluation_manifest(ROOT, args.suite,
                                                          gateway_for_manifest)
        destination = args.output or DEFAULT_REPORT_DIR / (
            'travel_quality_' + datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S') + '.json')
        save_report(report, destination)
        print('JSON report:', destination)
        print('Human review:', destination.with_suffix('.md'))
        if any(row.get('status') != 'PASS' for row in report['results']):
            raise SystemExit(2)
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        parser.exit(1, f'FAILED: {exc}\n')


if __name__ == '__main__':
    main()
