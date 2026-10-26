"""Opt-in, read-only checks for model profiles and travel data providers.

All targets come from trusted server catalogs. Requests never accept endpoint URLs,
credentials, arbitrary tool arguments, or HTTP headers from the browser.
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
from datetime import date, timedelta
from urllib.parse import urlsplit


async def check_model(name: str, gateway, *, allow_remote: bool = False) -> dict[str, str]:
    profile = next((item for item in gateway.profiles if item.get('name') == name), None)
    if profile is None:
        raise KeyError('Unknown model profile.')

    if not gateway._configured(profile):
        return _result('not_configured', 'Configure this model on the server before testing.')

    if profile.get('provider') == 'local-hf':
        if importlib.util.find_spec('sentence_transformers') is None:
            return _result('error', 'Local inference dependencies are missing. Install requirements/ml.txt.')
        return _result('configured',
                       'Local ML dependencies are installed. This does not verify model weights or inference. '
                       'Use the knowledge-library inference diagnostics for an end-to-end test.')

    if profile.get('provider') != 'openai-compatible':
        return _result('configured', 'This model provider does not support a live check here.')

    base_url = profile.get('base_url', '')
    local = urlsplit(base_url).hostname in ('localhost', '127.0.0.1', '::1')
    if not local and not allow_remote:
        return _result('configured', 'Remote checks require an explicit opt-in.')

    key_name = profile.get('api_key_env', '')
    secret = os.getenv(key_name, '') if key_name else ''
    if not secret:
        return _result('not_configured', 'A model credential is not configured.')

    try:
        from openai import AsyncOpenAI
        model = os.getenv(profile.get('model_env', ''), profile['model'])
        timeout = 55.0 if local else 20.0
        kwargs = {'max_completion_tokens': 32} if 'max_completion_tokens' in profile else {'max_tokens': 32}
        async with AsyncOpenAI(api_key=secret, base_url=base_url,
                               timeout=timeout, max_retries=0) as client:
            response = await asyncio.wait_for(
                client.chat.completions.create(
                    model=model,
                    messages=[{'role': 'user', 'content': 'Reply with OK.'}],
                    **kwargs),
                timeout=timeout + 2,
            )
        if not getattr(response, 'choices', None):
            return _result('error', 'The model endpoint returned no completion choices.')
        return _result('connected', 'A real chat-completion request succeeded.')
    except asyncio.TimeoutError:
        return _result('error', 'The model connection timed out.')
    except Exception as exc:
        # Do not return raw provider exceptions: URLs may include sensitive data.
        return _result('error', _safe_model_error(exc))


async def check_provider(name: str, gateway, *, allow_remote: bool = False) -> dict[str, str]:
    entry = next((p for p in gateway.catalog() if p['capability'] == name), None)
    if entry is None:
        raise KeyError('Unknown travel data service.')
    if not entry['configured']:
        return _result('not_configured', 'Online access or required service credentials are disabled.')
    if not allow_remote:
        return _result('configured', 'An external service check requires an explicit opt-in.')

    # Fixed, read-only smoke samples. No booking, purchase or mutable operation.
    examples = {
        'geocode': {'place': 'Paris'},
        'weather': {'latitude': 48.8566, 'longitude': 2.3522, 'forecast_days': 1},
        'places': {'latitude': 48.8566, 'longitude': 2.3522, 'kind': 'tourism', 'radius': 2000},
        'fx': {'amount': 10, 'origin': 'USD', 'destination': 'EUR'},
        'walking_route': {'waypoints': [
            {'latitude': 48.8566, 'longitude': 2.3522},
            {'latitude': 48.8586, 'longitude': 2.3542},
        ]},
        'flights': {'origin': 'LHR', 'destination': 'CDG',
                    'departure_date': (date.today() + timedelta(days=21)).isoformat(),
                    'passengers': 1},
    }
    if name not in examples:
        return _result('configured', 'No safe read-only smoke test is available for this service.')
    try:
        result = await asyncio.wait_for(gateway.request(name, **examples[name]), timeout=25)
        data = result.data
        if not isinstance(data, dict) or not _valid_provider_result(name, data):
            return _result('error', 'The service responded without the expected data structure.')
        return _result('connected', 'A read-only provider request returned a valid response.')
    except asyncio.TimeoutError:
        return _result('error', 'The service check timed out.')
    except Exception as exc:
        # Never echo exception strings, URLs, request headers, or credentials.
        return _result('error', f'Service check failed ({type(exc).__name__}).')


def _valid_provider_result(name: str, data: dict) -> bool:
    if name in ('geocode', 'places'):
        return isinstance(data.get('results'), list)  # An empty list is valid.
    if name == 'weather':
        hourly = data.get('hourly')
        return isinstance(hourly, dict) and bool(hourly.get('temperature_2m'))
    if name == 'fx':
        rate = data.get('rate')
        return isinstance(rate, (int, float)) and not isinstance(rate, bool) and rate > 0
    if name == 'walking_route':
        value = data.get('distance_m')
        return isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0
    if name == 'flights':
        return isinstance(data.get('offers'), list)  # No offers is not an API error.
    return False


def _safe_model_error(exc: Exception) -> str:
    name = type(exc).__name__
    if name == 'AuthenticationError':
        return 'The model service rejected the credentials.'
    if name == 'RateLimitError':
        return 'The model service rejected the request due to rate limits or quota.'
    if name == 'NotFoundError':
        return 'The model or endpoint was not found.'
    if name == 'BadRequestError':
        return 'The model rejected the test request or its parameters.'
    if name in ('APIConnectionError', 'ConnectError', 'ConnectionError'):
        return 'The model endpoint could not be reached.'
    return f'Model check failed ({name}).'


def _result(status: str, detail: str) -> dict[str, str]:
    return {'status': status, 'detail': detail}
