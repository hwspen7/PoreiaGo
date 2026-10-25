"""Tests for local model profile installation and idempotence."""
import importlib.util
import json
from pathlib import Path


def load_installer():
    script = Path(__file__).resolve().parents[2] / 'models' / 'configure_local_llm.py'
    spec = importlib.util.spec_from_file_location('configure_local_llm', script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_ollama_profiles_precede_cloud_profiles(tmp_path):
    path = tmp_path / 'models.json'
    path.write_text(json.dumps({'profiles': [
        {'name': 'groq-router', 'role': 'router', 'provider': 'openai-compatible',
         'model': 'remote', 'base_url': 'https://example.com', 'api_key_env': 'TEST_KEY'},
        {'name': 'retrieval', 'role': 'embedding', 'provider': 'local-hf', 'model': 'test-model'},
    ]}), encoding='utf-8')
    tool = load_installer()
    tool.configure_models(path)
    profiles = json.loads(path.read_text(encoding='utf-8'))['profiles']
    assert [p['name'] for p in profiles[:3]] == [
        'ollama-router', 'ollama-reasoning', 'ollama-synthesis']
    assert profiles[3]['name'] == 'groq-router'
    assert profiles[4]['name'] == 'retrieval'
    assert (tmp_path / 'backups' / 'models.before-ollama.json').exists()
    assert all(p['api_key_env'] == 'POREIAGO_OLLAMA_API_KEY' for p in profiles[:3])
    assert all(p['model_env'] == 'POREIAGO_LOCAL_MODEL' for p in profiles[:3])
    assert profiles[0]['reasoning_effort'] == 'none'
    assert 'reasoning_effort' not in profiles[1]
    assert 'reasoning_effort' not in profiles[2]


def test_ollama_installer_is_idempotent(tmp_path):
    path = tmp_path / 'models.json'
    path.write_text(json.dumps({'profiles': []}), encoding='utf-8')
    tool = load_installer()
    tool.configure_models(path)
    tool.configure_models(path)
    profiles = json.loads(path.read_text(encoding='utf-8'))['profiles']
    assert len(profiles) == 3


def test_ollama_installer_does_not_change_unknown_registry_fields(tmp_path):
    path = tmp_path / 'models.json'
    path.write_text(json.dumps({'profiles': [], 'revision': 7}), encoding='utf-8')
    load_installer().configure_models(path)
    assert json.loads(path.read_text(encoding='utf-8'))['revision'] == 7
