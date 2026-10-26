"""Offline contract tests for user-initiated connection probes."""
import pytest

from services.connection_status import check_model, check_provider


class Models:
    def __init__(self, configured):
        self.profiles = [
            {'name': 'sample', 'provider': 'openai-compatible', 'model': 'example',
             'base_url': 'https://api.example.invalid/v1', 'api_key_env': 'TEST_NEVER_SET_SECRET'},
            {'name': 'local-embed', 'provider': 'local-hf', 'model': 'example'},
        ]
        self.is_configured = configured

    def _configured(self, profile):
        return self.is_configured


class Providers:
    def __init__(self, configured=True, data=None):
        self.configured = configured
        self.data = data if data is not None else {'hourly': {'temperature_2m': [15]}}
        self.calls = []

    def catalog(self):
        return [{'capability': 'weather', 'provider': 'openmeteo', 'configured': self.configured}]

    async def request(self, capability, **kwargs):
        self.calls.append((capability, kwargs))
        return type('Response', (), {'data': self.data})()


@pytest.mark.asyncio
async def test_unconfigured_model_never_probes():
    result = await check_model('sample', Models(False), allow_remote=True)
    assert result['status'] == 'not_configured'


@pytest.mark.asyncio
async def test_remote_model_requires_explicit_opt_in():
    result = await check_model('sample', Models(True), allow_remote=False)
    assert result['status'] == 'configured'


@pytest.mark.asyncio
async def test_unknown_models_and_services_rejected():
    with pytest.raises(KeyError):
        await check_model('unknown', Models(True), allow_remote=True)
    with pytest.raises(KeyError):
        await check_provider('unknown', Providers(), allow_remote=True)


@pytest.mark.asyncio
async def test_provider_requires_permission_and_valid_response():
    service = Providers()
    pending = await check_provider('weather', service)
    assert pending['status'] == 'configured'
    assert not service.calls

    checked = await check_provider('weather', service, allow_remote=True)
    assert checked['status'] == 'connected'
    assert len(service.calls) == 1
    assert service.calls[0][0] == 'weather'


@pytest.mark.asyncio
async def test_provider_error_and_unconfigured_do_not_leak_raw_exception():
    service = Providers(data={})
    checked = await check_provider('weather', service, allow_remote=True)
    assert checked['status'] == 'error'

    service = Providers(configured=False)
    checked = await check_provider('weather', service, allow_remote=True)
    assert checked['status'] == 'not_configured'
    assert not service.calls
