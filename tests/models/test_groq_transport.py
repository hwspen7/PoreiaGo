"""Validate OpenAI-compatible cloud payloads without real credentials or network calls."""
import asyncio
import json
from dataclasses import replace
from pathlib import Path
import sys
import types
import pytest

from core.config import settings
from intelligence.models import ModelGateway


@pytest.mark.asyncio
async def test_groq_openai_compatible_transport(monkeypatch):
    calls = []
    class FakeCreate:
        async def create(self, **kwargs):
            calls.append(kwargs)
            return types.SimpleNamespace(choices=[types.SimpleNamespace(message=types.SimpleNamespace(content='{"mode":"direct","answer":"OK"}'))])
    class FakeClient:
        def __init__(self, **kwargs):
            assert kwargs['api_key'] == 'mock-key'
            assert kwargs['base_url'] == 'https://api.groq.com/openai/v1'
            self.chat=types.SimpleNamespace(completions=FakeCreate())
    monkeypatch.setitem(sys.modules,'openai',types.SimpleNamespace(AsyncOpenAI=FakeClient))
    monkeypatch.setenv('GROQ_API_KEY','mock-key')
    models=ModelGateway(settings)
    result=await models.complete('router', 'system', 'query', json_mode=True)
    assert result == {'mode':'direct','answer':'OK'}
    assert calls[0]['model']=='qwen/qwen3.8-27b'
    assert calls[0]['extra_body']['max_completion_tokens'] == 3200
    assert calls[0]['extra_body']['reasoning_effort'] == 'none'
    assert calls[0]['response_format']=={'type':'json_object'}
    assert calls[0]['messages'][1]['content']=='query'
