"""Fail before inference when the installed Ollama router is stale."""
import pytest

from scripts import diagnose_model_planning as diag


class FakeRouterGateway:
    def __init__(self, profile):
        self.profiles = [profile]

    def configured(self, role):
        return True

    def _configured(self, profile):
        return True


@pytest.mark.asyncio
async def test_stale_router_profile_exits_without_model_call(monkeypatch, capsys):
    gateway = FakeRouterGateway({
        'name': 'ollama-router', 'role': 'router', 'provider': 'openai-compatible',
        'decision_only': True, 'max_output_tokens': 1536,
    })
    monkeypatch.setattr(diag, 'ModelGateway', lambda settings: gateway)
    assert await diag.diagnose() == 1
    text = capsys.readouterr().out
    assert 'stale Ollama router profile' in text
    assert 'Router reasoning effort: default' in text
