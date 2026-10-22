"""Keep deterministic unit tests independent of developer cloud credentials.

Live model integration is checked separately by explicit smoke tests. Unit tests
must never depend on a developer's local .env or consume provider credits.
"""
from __future__ import annotations

import json
import pytest

from aligo.config import settings


def _model_credential_names() -> set[str]:
    """Discover all cloud credentials from the active model profile registry."""
    config = json.loads(settings.models_file.read_text(encoding='utf-8'))
    return {
        profile['api_key_env']
        for profile in config.get('profiles', [])
        if profile.get('provider') == 'openai-compatible'
        and isinstance(profile.get('api_key_env'), str)
        and profile['api_key_env'].strip()
    }


@pytest.fixture(autouse=True)
def isolate_cloud_models(monkeypatch):
    """Disable ambient API keys; individual mocked-transport tests can opt in.

    ``aligo.config`` loads the local ``.env`` at import time. Clearing only
    environment variables in a shell command is not sufficient for reliable
    tests, because this file may reload the original secrets. This fixture
    runs after imports and before each test, so ModelGateway sees no implicit
    cloud credentials. Tests may still set their own harmless mock keys after
    the fixture executes.
    """
    for name in _model_credential_names():
        monkeypatch.delenv(name, raising=False)
    # Keep assertions stable if the developer selects a different local or
    # hosted model in .env. Tests that need overrides set them explicitly.
    for name in ('ALIGO_GROQ_MODEL', 'ALIGO_EMBEDDING_MODEL', 'ALIGO_RERANKER_MODEL',
                 'ALIGO_CONTENT_REVIEW_MODE'):
        monkeypatch.delenv(name, raising=False)
    # Content review is an explicitly enabled, optional second LLM pass.
    # The developer's .env may enable it for real users, but baseline tests
    # assert single-pass routing or repair. Opt-in tests set the mode locally.
