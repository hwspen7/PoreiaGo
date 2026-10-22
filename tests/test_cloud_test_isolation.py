"""Regression checks for safe, deterministic cloud-model test isolation."""

import os

from aligo.config import settings
from aligo.models import ModelGateway


def test_real_model_credentials_are_disabled_during_unit_tests():
    gateway = ModelGateway(settings)
    names = {profile["api_key_env"] for profile in gateway.profiles
             if profile.get("provider") == "openai-compatible"}
    assert names
    assert all(not os.getenv(name) for name in names)
    assert not gateway.configured('router')
    assert not gateway.configured('reasoning')
    assert not gateway.configured('synthesis')
