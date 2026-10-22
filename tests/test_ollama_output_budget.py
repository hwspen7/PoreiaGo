"""Regression tests for Ollama output length and compact itinerary planning."""
import asyncio
import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from aligo.models import ModelGateway, ModelUnavailable
from aligo.model_agent import SYSTEM


def test_create_prompt_keeps_json_only():
    assert "set 'answer' to an empty string" in SYSTEM
    assert 'Do not duplicate the itinerary as prose.' in SYSTEM


def test_output_budgets_are_installed_and_idempotent(tmp_path):
    from scripts.configure_local_llm import configure_models
    path = tmp_path / 'models.json'
    path.write_text(json.dumps({'profiles': []}), encoding='utf-8')
    configure_models(path)
    configure_models(path)
    entries = json.loads(path.read_text(encoding='utf-8'))['profiles']
    assert len(entries) == 3
    assert [(item['role'], item['max_output_tokens']) for item in entries] == [
        ('router', 3072), ('reasoning', 8192), ('synthesis', 8192)]


@pytest.mark.asyncio
async def test_ollama_uses_standard_max_tokens(monkeypatch, tmp_path):
    captured = []

    class StubCompletions:
        async def create(self, **kwargs):
            captured.append(kwargs)
            return SimpleNamespace(choices=[SimpleNamespace(
                finish_reason='stop', message=SimpleNamespace(content='{"mode":"direct"}'))])

    class StubAsyncOpenAI:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=StubCompletions())

    monkeypatch.setitem(sys.modules, 'openai', types.SimpleNamespace(AsyncOpenAI=StubAsyncOpenAI))
    monkeypatch.setenv('ALIGO_OLLAMA_API_KEY', 'ollama')
    profiles = {'profiles': [{'name':'ollama-router','role':'router',
               'provider':'openai-compatible','model':'gemma4:12b-it-qat',
               'base_url':'http://127.0.0.1:11434/v1',
               'api_key_env':'ALIGO_OLLAMA_API_KEY',
               'max_output_tokens': 12288}]}
    p = tmp_path / 'models.json'
    p.write_text(json.dumps(profiles), encoding='utf-8')
    gw = ModelGateway(SimpleNamespace(models_file=p, request_timeout=12))
    got = await gw.complete('router', 'system', 'prompt', json_mode=True)
    assert got == {'mode': 'direct'}
    assert captured[0]['max_tokens'] == 12288
    assert captured[0]['response_format'] == {'type':'json_object'}
    assert 'extra_body' not in captured[0]


@pytest.mark.asyncio
async def test_truncated_output_still_fails_safely(monkeypatch, tmp_path):
    class StubCompletions:
        async def create(self, **kwargs):
            return SimpleNamespace(choices=[SimpleNamespace(
                finish_reason='length', message=SimpleNamespace(content='{"incomplete":'))])
    class StubAsyncOpenAI:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=StubCompletions())
    monkeypatch.setitem(sys.modules, 'openai', types.SimpleNamespace(AsyncOpenAI=StubAsyncOpenAI))
    monkeypatch.setenv('ALIGO_OLLAMA_API_KEY', 'ollama')
    path = tmp_path / 'models.json'
    path.write_text(json.dumps({'profiles':[{'name':'ollama-router','role':'router',
                 'provider':'openai-compatible','model':'gemma4:12b-it-qat',
                 'base_url':'http://127.0.0.1:11434/v1',
                 'api_key_env':'ALIGO_OLLAMA_API_KEY','max_output_tokens':12288}]}), encoding='utf-8')
    gw = ModelGateway(SimpleNamespace(models_file=path, request_timeout=12))
    with pytest.raises(ModelUnavailable, match='OUTPUT_TRUNCATED'):
        await gw.complete('router', 'system', 'prompt', json_mode=True)


@pytest.mark.asyncio
async def test_router_disables_thinking_without_changing_other_roles(monkeypatch, tmp_path):
    from scripts.configure_local_llm import configure_models
    seen = []

    class FakeCompletions:
        async def create(self, **kwargs):
            seen.append(kwargs)
            return SimpleNamespace(choices=[SimpleNamespace(
                finish_reason='stop', message=SimpleNamespace(content='{"mode":"direct"}'))])

    class FakeOpenAI:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setitem(sys.modules, 'openai', types.SimpleNamespace(AsyncOpenAI=FakeOpenAI))
    monkeypatch.setenv('ALIGO_OLLAMA_API_KEY', 'ollama')
    path = tmp_path / 'models.json'
    path.write_text(json.dumps({'profiles': []}), encoding='utf-8')
    configure_models(path)
    gw = ModelGateway(SimpleNamespace(models_file=path, request_timeout=12))
    await gw.complete('router', 'system', 'prompt', json_mode=True)
    assert seen[0]['max_tokens'] == 3072
    assert seen[0]['extra_body']['reasoning_effort'] == 'none'
    assert gw.router_decision_only() is True
    await gw.complete('reasoning', 'system', 'prompt', json_mode=True)
    assert seen[1]['max_tokens'] == 8192
    assert 'extra_body' not in seen[1]
