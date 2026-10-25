"""Model-independent stage routing and reproducible benchmark contracts."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
import sys
import types

import pytest

from quality.benchmark_manifest import evaluation_manifest
from intelligence.model_presets import load_preset
from intelligence.models import ModelGateway
from evaluation import evaluate_travel_quality as quality


def make_gateway(tmp_path, monkeypatch, *, stage_map=None, profiles=None):
    registry = profiles or [
        {'name': 'local-router', 'role': 'router', 'provider': 'openai-compatible',
         'model': 'router-small', 'model_env': 'POREIAGO_LOCAL_MODEL',
         'base_url': 'http://127.0.0.1:11434/v1', 'api_key_env': 'LOCAL_KEY',
         'max_output_tokens': 3072, 'decision_only': True},
        {'name': 'local-reasoning', 'role': 'reasoning', 'provider': 'openai-compatible',
         'model': 'base', 'model_env': 'POREIAGO_LOCAL_MODEL',
         'base_url': 'http://127.0.0.1:11434/v1', 'api_key_env': 'LOCAL_KEY',
         'max_output_tokens': 8192},
        {'name': 'local-synthesis', 'role': 'synthesis', 'provider': 'openai-compatible',
         'model': 'base', 'model_env': 'POREIAGO_LOCAL_MODEL',
         'base_url': 'http://127.0.0.1:11434/v1', 'api_key_env': 'LOCAL_KEY',
         'max_output_tokens': 8192},
    ]
    stages = stage_map or {
        'router': 'local-router', 'reasoning': 'local-reasoning',
        'synthesis': 'local-synthesis', 'blueprint': 'local-reasoning',
        'daily': 'local-reasoning', 'repair': 'local-reasoning',
    }
    (tmp_path / 'models.json').write_text(json.dumps({'profiles': registry}), encoding='utf-8')
    (tmp_path / 'model_presets.json').write_text(json.dumps({
        'version': 1, 'presets': {'trial': {'stages': stages}}}), encoding='utf-8')
    monkeypatch.setenv('LOCAL_KEY', 'test-key')
    monkeypatch.setenv('POREIAGO_MODEL_PRESETS_FILE', str(tmp_path / 'model_presets.json'))
    monkeypatch.delenv('POREIAGO_MODEL_PRESET', raising=False)
    return ModelGateway(SimpleNamespace(models_file=tmp_path / 'models.json', request_timeout=10),
                        preset='trial')


def test_six_stages_are_required_and_roles_must_match(tmp_path, monkeypatch):
    gateway = make_gateway(tmp_path, monkeypatch)
    assert len(gateway.selected_profiles()) == 6
    file = tmp_path / 'model_presets.json'
    preset = json.loads(file.read_text(encoding='utf-8'))
    del preset['presets']['trial']['stages']['repair']
    file.write_text(json.dumps(preset), encoding='utf-8')
    with pytest.raises(ValueError, match='all six'):
        make = ModelGateway(SimpleNamespace(models_file=tmp_path / 'models.json', request_timeout=10), preset='trial')
    preset['presets']['trial']['stages']['repair'] = 'local-router'
    file.write_text(json.dumps(preset), encoding='utf-8')
    with pytest.raises(ValueError, match='Invalid repair profile'):
        ModelGateway(SimpleNamespace(models_file=tmp_path / 'models.json', request_timeout=10), preset='trial')


def test_override_precedence_and_no_cross_profile_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv('POREIAGO_LOCAL_MODEL', 'old-model')
    stages = {'router': 'local-router', 'reasoning': 'local-reasoning',
              'synthesis': 'local-synthesis', 'blueprint': {'profile': 'local-reasoning', 'model': 'larger-model'},
              'daily': {'profile': 'local-reasoning', 'model': 'day-model', 'temperature': 0.6,
                        'max_output_tokens': 20000, 'disable_reasoning': False},
              'repair': {'profile': 'local-reasoning', 'model': 'repair-model'}}
    gw = make_gateway(tmp_path, monkeypatch, stage_map=stages)
    assert gw.selected_profiles()['router']['model'] == 'old-model'
    assert gw.selected_profiles()['blueprint']['model'] == 'larger-model'
    assert gw.selected_profiles()['daily']['model'] == 'day-model'
    assert len(gw._candidate_profiles('reasoning', 'daily')) == 1
    assert gw._candidate_profiles('reasoning', 'daily')[0]['temperature'] == .6
    assert gw._candidate_profiles('reasoning', 'daily')[0]['max_output_tokens'] == 20000
    assert gw._candidate_profiles('reasoning', 'daily')[0]['disable_reasoning'] is False


def test_disallow_credential_injection_and_invalid_options(tmp_path, monkeypatch):
    gw = make_gateway(tmp_path, monkeypatch)
    config = tmp_path / 'model_presets.json'
    data = json.loads(config.read_text(encoding='utf-8'))
    data['presets']['trial']['stages']['daily'] = {'profile': 'local-reasoning', 'api_key_env': 'EXFILTRATE'}
    config.write_text(json.dumps(data), encoding='utf-8')
    with pytest.raises(ValueError, match='Unsupported'):
        load_preset(config, 'trial', gw.profiles)
    data['presets']['trial']['stages']['daily'] = {'profile': 'local-reasoning', 'max_output_tokens': -10}
    config.write_text(json.dumps(data), encoding='utf-8')
    with pytest.raises(ValueError, match='Invalid max_output_tokens'):
        load_preset(config, 'trial', gw.profiles)


@pytest.mark.asyncio
async def test_independent_stage_models_reach_real_completion_adapter(tmp_path, monkeypatch):
    stages = {'router': 'local-router', 'reasoning': 'local-reasoning',
              'synthesis': 'local-synthesis',
              'blueprint': {'profile': 'local-reasoning', 'model': 'blueprint-large'},
              'daily': {'profile': 'local-reasoning', 'model': 'daily-large'},
              'repair': {'profile': 'local-reasoning', 'model': 'repair-large'}}
    gw = make_gateway(tmp_path, monkeypatch, stage_map=stages)
    calls = []
    class FakeCreate:
        async def create(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(choices=[SimpleNamespace(finish_reason='stop',
                message=SimpleNamespace(content='{"days": []}'))],
                usage=SimpleNamespace(prompt_tokens=120, completion_tokens=15, total_tokens=135))
    class FakeClient:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=FakeCreate())
    monkeypatch.setitem(sys.modules, 'openai', types.SimpleNamespace(AsyncOpenAI=FakeClient))
    await gw.complete_global_blueprint('test', '{}', day_count=3)
    assert gw.completion_metadata()['stage'] == 'blueprint'
    await gw.complete_daily_detail('test', '{}', day_number=1)
    assert gw.completion_metadata()['stage'] == 'daily'
    await gw.complete_activity_repair('test', '{}', day_number=1)
    assert [call['model'] for call in calls] == ['blueprint-large', 'daily-large', 'repair-large']
    assert all(call['response_format']['type'] == 'json_schema' for call in calls)


@pytest.mark.asyncio
async def test_cloud_preset_is_refused_without_explicit_permission(monkeypatch, tmp_path):
    cloud = [{'name': 'cloud', 'role': 'reasoning', 'provider': 'openai-compatible',
              'model': 'large-cloud', 'base_url': 'https://api.example.com/v1',
              'api_key_env': 'SECRET_CLOUD'},
             {'name': 'router', 'role': 'router', 'provider': 'openai-compatible',
              'model': 'r', 'base_url': 'http://127.0.0.1:11434/v1',
              'api_key_env': 'POREIAGO_OLLAMA_API_KEY'},
             {'name': 'synthesis', 'role': 'synthesis', 'provider': 'openai-compatible',
              'model': 's', 'base_url': 'http://127.0.0.1:11434/v1',
              'api_key_env': 'POREIAGO_OLLAMA_API_KEY'}]
    (tmp_path / 'models.json').write_text(json.dumps({'profiles': cloud}), encoding='utf-8')
    (tmp_path / 'model_presets.json').write_text(json.dumps({'version': 1, 'presets':
        {'cloud': {'stages': {'router': 'router', 'reasoning': 'cloud',
                              'synthesis': 'synthesis', 'blueprint': 'cloud',
                              'daily': 'cloud', 'repair': 'cloud'}}}}), encoding='utf-8')
    class StubModelGateway(ModelGateway):
        def __init__(self, settings, *, preset=None):
            super().__init__(SimpleNamespace(models_file=tmp_path / 'models.json', request_timeout=10),
                             preset=preset)
    monkeypatch.setenv('POREIAGO_MODEL_PRESETS_FILE', str(tmp_path / 'model_presets.json'))
    monkeypatch.setattr(quality, 'ModelGateway', StubModelGateway)
    monkeypatch.setattr(quality, 'installed_models', lambda: (_ for _ in ()).throw(AssertionError('must not call local')))  # No inference.
    with pytest.raises(ValueError, match='allow-remote-models'):
        await quality.run_model_preset([{'id': 'x', 'request': 'trip', 'kind': 'create'}], 'cloud')


def test_manifest_fingerprints_change_only_with_inputs(tmp_path, monkeypatch):
    gw = make_gateway(tmp_path, monkeypatch)
    root = tmp_path / 'project'
    (root / 'intelligence').mkdir(parents=True)
    (root / 'intelligence' / 'models.py').write_text('model = 1', encoding='utf-8')
    (root / 'config').mkdir()
    (root / 'config' / 'models.json').write_text('{"profiles":[]}', encoding='utf-8')
    suite = root / 'suite.json'
    suite.write_text('{"cases":[]}', encoding='utf-8')
    first = evaluation_manifest(root, suite, gw)
    second = evaluation_manifest(root, suite, gw)
    assert first['fingerprint'] == second['fingerprint']
    assert first['selected_stages']['daily']['profile'] == 'local-reasoning'
    suite.write_text("{\"cases\":[1]}", encoding="utf-8")
    assert evaluation_manifest(root, suite, gw)['fingerprint'] != first['fingerprint']

@pytest.mark.asyncio
async def test_local_preset_report_tracks_selected_models_without_cloud_calls(tmp_path, monkeypatch):
    stages = {'router': 'local-router', 'reasoning': 'local-reasoning',
              'synthesis': 'local-synthesis',
              'blueprint': {'profile': 'local-reasoning', 'model': 'big'},
              'daily': {'profile': 'local-reasoning', 'model': 'big'},
              'repair': 'local-reasoning'}
    # Real users may leave POREIAGO_LOCAL_MODEL set in their PowerShell session.
    # The synthetic Ollama catalog below contains only fixture models, so the
    # test must not inherit the user's actual installed-model selection.
    monkeypatch.delenv('POREIAGO_LOCAL_MODEL', raising=False)
    make_gateway(tmp_path, monkeypatch, stage_map=stages)
    class TestGateway(ModelGateway):
        def __init__(self, settings, *, preset=None):
            super().__init__(SimpleNamespace(models_file=tmp_path / 'models.json', request_timeout=10),
                             preset=preset)
    monkeypatch.setattr(quality, 'ModelGateway', TestGateway)
    monkeypatch.setattr(quality, 'installed_models', lambda: {'router-small', 'base', 'big'})
    calls = []
    async def fake_evaluate(case, gateway, registry, saved, *, providers=None, skill_mode='model_only'):
        calls.append(gateway.selected_profiles()['daily']['model'])
        return {'case_id': case['id'], 'status': 'PASS', 'score': 100,
                'elapsed_seconds': 0, 'warnings': [], 'passed_structure': True}, None
    monkeypatch.setattr(quality, 'evaluate_case', fake_evaluate)
    data = await quality.run_model_preset([{'id': 'scenario', 'request': 'A trip', 'kind': 'create'}],
                                          'trial', repeats=2, mode='model')
    assert data['model_preset'] == 'trial'
    assert data['stage_models']['daily']['model'] == 'big'
    assert calls == ['big', 'big']
    assert all(row['model'] == 'trial' for row in data['results'])


def test_report_stage_metadata_does_not_include_credentials(tmp_path, monkeypatch):
    gateway = make_gateway(tmp_path, monkeypatch)
    selection = gateway.selected_profiles()
    assert all('api_key' not in str(value) for value in selection.values())
    assert all('base_url' not in value for value in selection.values())
    assert selection['daily']['endpoint'] == 'ollama-local'

@pytest.mark.asyncio
async def test_explicit_stage_budgets_are_not_artificially_capped(tmp_path, monkeypatch):
    stages = {'router': 'local-router', 'reasoning': 'local-reasoning',
              'synthesis': 'local-synthesis',
              'blueprint': {'profile': 'local-reasoning', 'max_output_tokens': 12000},
              'daily': {'profile': 'local-reasoning', 'max_output_tokens': 16000},
              'repair': {'profile': 'local-reasoning', 'max_output_tokens': 6000}}
    gateway = make_gateway(tmp_path, monkeypatch, stage_map=stages)
    seen = []
    class FakeCreate:
        async def create(self, **kwargs):
            seen.append(kwargs)
            return SimpleNamespace(choices=[SimpleNamespace(finish_reason='stop',
                   message=SimpleNamespace(content='{}'))])
    class FakeClient:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=FakeCreate())
    monkeypatch.setitem(sys.modules, 'openai', types.SimpleNamespace(AsyncOpenAI=FakeClient))
    await gateway.complete_global_blueprint('sys', '{}', day_count=3)
    await gateway.complete_daily_detail('sys', '{}', day_number=1)
    await gateway.complete_activity_repair('sys', '{}', day_number=1)
    assert [item['max_tokens'] for item in seen] == [12000, 16000, 6000]
