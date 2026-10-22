"""Smoke-tool parser and security tests, without model inference."""
from __future__ import annotations

import json

import pytest

from scripts.verify_frontend_delivery import CORE_SKILLS, _events, _local_address, main


def test_frontend_sse_decoder_accepts_multiline_data_and_crlf():
    content = ['event: plan\r', 'data: {"skills": []}\r', '\r',
               'event: done\r', 'data: {"answer": "OK",\r',
               'data: "active_itinerary": null}\r', '\r']
    assert list(_events(content)) == [
        ('plan', {'skills': []}), ('done', {'answer': 'OK', 'active_itinerary': None})]


def test_smoke_rejects_external_websites_and_embedded_credentials():
    for url in ['https://example.com', 'http://127.0.0.1.evil.tld',
                'http://token:password@localhost:8000']:
        with pytest.raises(ValueError):
            _local_address(url)
    assert _local_address('http://127.0.0.1:8000') == 'http://127.0.0.1:8000'
    assert len(CORE_SKILLS) == 18


def test_cli_preflight_does_not_start_live_inference(monkeypatch, capsys):
    class Response:
        status_code = 200
        headers = {'content-type': 'text/html'}
        text = '<script type="module" src="/assets/index.js"></script>'
        def json(self):
            return self._data
        def raise_for_status(self):
            return None
    class Client:
        def __init__(self, *args, **kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def get(self, url, **kwargs):
            res = Response()
            if url.endswith('/api/health'):
                res._data = {'status': 'ok', 'models': [{'role': 'router', 'configured': True},
                                                         {'role': 'reasoning', 'configured': True}]}
                res.headers = {'content-type': 'application/json'}
            elif url.endswith('/api/skills'):
                res._data = {'skills': [{'id': name} for name in CORE_SKILLS]}
                res.headers = {'content-type': 'application/json'}
            return res
        def stream(self, *args, **kwargs):
            raise AssertionError('Preflight must not start inference.')
    import scripts.verify_frontend_delivery as module
    monkeypatch.setattr(module.httpx, 'Client', Client)
    assert main([]) == 0
    assert 'FRONTEND DELIVERY PASS' in capsys.readouterr().out
