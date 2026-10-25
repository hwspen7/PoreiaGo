"""Canonical PoreiaGo environment and admin-token compatibility regressions."""
from __future__ import annotations

import os
import subprocess
import sys

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from core.security import require_admin_token


def test_legacy_env_is_read_when_new_name_is_absent():
    env = os.environ.copy()
    env.pop('POREIAGO_ACCESS_TOKEN', None)
    env['ALIGO_ACCESS_TOKEN'] = 'legacy-example-only'
    code = 'from core.config import settings; print(settings.access_token)'
    run = subprocess.run([sys.executable, '-c', code], env=env,
                         text=True, capture_output=True, check=True)
    assert run.stdout.strip() == 'legacy-example-only'


def test_canonical_env_takes_precedence_over_legacy():
    env = os.environ.copy()
    env['POREIAGO_ACCESS_TOKEN'] = 'canonical-example-only'
    env['ALIGO_ACCESS_TOKEN'] = 'legacy-example-only'
    code = 'from core.config import settings; print(settings.access_token)'
    run = subprocess.run([sys.executable, '-c', code], env=env,
                         text=True, capture_output=True, check=True)
    assert run.stdout.strip() == 'canonical-example-only'


def _request(header_name: str, value: str) -> Request:
    return Request({'type': 'http', 'method': 'GET', 'path': '/', 'query_string': b'',
                    'headers': [(header_name.lower().encode(), value.encode())]})


def test_canonical_and_legacy_admin_header(monkeypatch):
    monkeypatch.setenv('POREIAGO_ENABLE_MODEL_ADMIN', '1')
    monkeypatch.setenv('POREIAGO_MODEL_ADMIN_TOKEN', 'example-only-admin')
    for header in ('X-PoreiaGo-Admin-Token', 'X-Aligo-Admin-Token'):
        require_admin_token(_request(header, 'example-only-admin'),
                            'POREIAGO_ENABLE_MODEL_ADMIN', 'POREIAGO_MODEL_ADMIN_TOKEN')
    with pytest.raises(HTTPException) as error:
        require_admin_token(_request('X-PoreiaGo-Admin-Token', 'wrong-token'),
                            'POREIAGO_ENABLE_MODEL_ADMIN', 'POREIAGO_MODEL_ADMIN_TOKEN')
    assert error.value.status_code == 401
    mixed_headers = Request({
        'type': 'http', 'method': 'GET', 'path': '/', 'query_string': b'',
        'headers': [
            (b'x-poreiago-admin-token', b'wrong-token'),
            (b'x-aligo-admin-token', b'example-only-admin'),
        ],
    })
    with pytest.raises(HTTPException) as mixed:
        require_admin_token(mixed_headers, 'POREIAGO_ENABLE_MODEL_ADMIN',
                            'POREIAGO_MODEL_ADMIN_TOKEN')
    assert mixed.value.status_code == 401
