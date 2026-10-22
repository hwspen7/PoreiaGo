"""Opt-in, durable administration of OpenAI-compatible model profiles.

Configuration changes are atomic. A new gateway is created for every activation
so in-flight requests can finish on the gateway they began with.
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from .model_presets import load_preset, list_presets
from .models import ModelGateway, ModelUnavailable, _is_loopback_ollama

_NAME = re.compile(r'^[A-Za-z][A-Za-z0-9_-]{0,79}$')
_ENV = re.compile(r'^[A-Z][A-Z0-9_]{0,99}$')
_ALLOWED_PROFILE_FIELDS = frozenset({
    'name', 'role', 'provider', 'model', 'base_url', 'api_key_env',
    'model_env', 'request_timeout_seconds', 'temperature', 'max_output_tokens',
    'max_completion_tokens', 'reasoning_effort', 'disable_reasoning',
    'decision_only',
})


def _local_endpoint(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        return (parsed.scheme in {'http', 'https'} and
                parsed.hostname in {'localhost', '127.0.0.1', '::1'} and
                parsed.username is None and parsed.password is None)
    except (ValueError, TypeError):
        return False


def _safe_endpoint(url: str) -> None:
    if not isinstance(url, str) or len(url) > 500:
        raise ValueError('A supported base_url is required.')
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        port = parsed.port
        if (not host or parsed.username or parsed.password or parsed.query or parsed.fragment
                or (port is not None and not 1 <= port <= 65535)):
            raise ValueError('Invalid model endpoint URL.')
        if _local_endpoint(url):
            return
        if parsed.scheme != 'https':
            raise ValueError('Remote model endpoints must use HTTPS.')
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            raise ValueError('Private network targets require a loopback endpoint.')
        if host.casefold().endswith(('.localhost', '.local', '.internal')):
            raise ValueError('Internal hostnames cannot be registered as remote endpoints.')
    except (ValueError, TypeError) as exc:
        raise ValueError(str(exc) or 'Invalid model endpoint URL.') from None


def _atomic_json(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.aligo-model-', suffix='.json', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
            json.dump(document, stream, ensure_ascii=True, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _read_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value, dict):
        raise ValueError('Model catalog must be a JSON object.')
    return value


def _validate_profile(profile: dict) -> dict:
    if not isinstance(profile, dict) or set(profile) - _ALLOWED_PROFILE_FIELDS:
        raise ValueError('Unsupported model profile fields.')
    if not isinstance(profile.get('name'), str) or not _NAME.fullmatch(profile['name']):
        raise ValueError('Invalid model profile name.')
    if profile.get('role') not in {'router', 'reasoning', 'synthesis'}:
        raise ValueError('Managed profiles must use router, reasoning, or synthesis roles.')
    if profile.get('provider') != 'openai-compatible':
        raise ValueError('Managed profiles require the openai-compatible provider.')
    if not isinstance(profile.get('model'), str) or not 1 <= len(profile['model'].strip()) <= 200:
        raise ValueError('Invalid model identifier.')
    _safe_endpoint(profile.get('base_url'))
    if not isinstance(profile.get('api_key_env'), str) or not _ENV.fullmatch(profile['api_key_env']):
        raise ValueError('Use an environment variable name for API credentials.')
    if 'model_env' in profile and (not isinstance(profile['model_env'], str) or
                                   not _ENV.fullmatch(profile['model_env'])):
        raise ValueError('Invalid model override environment variable.')
    if 'max_output_tokens' in profile and 'max_completion_tokens' in profile:
        raise ValueError('Use only one output token budget format.')
    for field in ('max_output_tokens', 'max_completion_tokens'):
        if field in profile and (type(profile[field]) is not int or not 1 <= profile[field] <= 1000000):
            raise ValueError(f'Invalid {field}.')
    for field in ('request_timeout_seconds',):
        if field in profile and (type(profile[field]) not in (int, float) or not 1 <= profile[field] <= 3600):
            raise ValueError(f'Invalid {field}.')
    if 'temperature' in profile and (type(profile['temperature']) not in (int, float) or
                                     not 0 <= profile['temperature'] <= 2):
        raise ValueError('Invalid temperature.')
    for field in ('disable_reasoning', 'decision_only'):
        if field in profile and type(profile[field]) is not bool:
            raise ValueError(f'Invalid {field}.')
    if 'reasoning_effort' in profile and (not isinstance(profile['reasoning_effort'], str) or
                                        len(profile['reasoning_effort']) > 40):
        raise ValueError('Invalid reasoning effort.')
    return dict(profile)


class ModelControl:
    """Config file management and independent gateway snapshots."""

    def __init__(self, settings, *, presets_file=None, state_file=None):
        self.settings = settings
        self.models_file = Path(settings.models_file)
        self.presets_file = Path(presets_file or os.getenv('ALIGO_MODEL_PRESETS_FILE') or
                                 self.models_file.parent / 'model_presets.json')
        self.state_file = Path(state_file or Path(settings.db_path).parent / 'active_model_preset.json')
        stored = _read_json(self.state_file) if self.state_file.exists() else {}
        self.active_preset = (os.getenv('ALIGO_MODEL_PRESET', '').strip() or
                              stored.get('preset') or None)
        self.remote_authorized = bool(stored.get('remote_authorized', False))
        self.gateway = self.build_gateway(self.active_preset)

    def build_gateway(self, preset):
        # Explicitly avoid automatic ALIGO_MODEL_PRESET inheritance for legacy mode.
        if preset is None and os.getenv('ALIGO_MODEL_PRESET', '').strip():
            raise ValueError('Unset ALIGO_MODEL_PRESET before activating legacy routing.')
        return ModelGateway(self.settings, preset=preset, presets_path=self.presets_file)

    def presets(self):
        return list_presets(self.presets_file)

    def public_state(self) -> dict:
        return {'active_preset': self.active_preset,
                'stage_models': self.gateway.selected_profiles(),
                'profiles': self.gateway.catalog(),
                'presets': self.presets(),
                'protocols': ['openai-compatible-chat-completions'],
                'native_provider_adapters': [],
                'management_enabled': False}

    def add_profile(self, spec: dict):
        profile = _validate_profile(spec)
        data = _read_json(self.models_file)
        profiles = data.get('profiles')
        if not isinstance(profiles, list):
            raise ValueError('Invalid model profile catalog.')
        if any(p.get('name') == profile['name'] for p in profiles):
            raise ValueError('Model profile already exists.')
        data['profiles'] = [*profiles, profile]
        _atomic_json(self.models_file, data)
        return {'name': profile['name'], 'role': profile['role'], 'provider': profile['provider']}

    def remove_profile(self, name: str):
        if not _NAME.fullmatch(name):
            raise ValueError('Invalid model profile name.')
        data = _read_json(self.models_file)
        profiles = data.get('profiles', [])
        current = next((p for p in profiles if p.get('name') == name), None)
        if current is None:
            raise KeyError(name)
        for preset in _read_json(self.presets_file).get('presets', {}).values():
            for stage in preset.get('stages', {}).values():
                if (stage == name or (isinstance(stage, dict) and stage.get('profile') == name)):
                    raise ValueError('Model profile is referenced by a preset.')
        if self.active_preset is None:
            raise ValueError('Profiles cannot be removed while legacy routing is active.')
        data['profiles'] = [p for p in profiles if p.get('name') != name]
        _atomic_json(self.models_file, data)
        return {'removed': name}

    def add_preset(self, name: str, stages: dict, *, description=''):
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            raise ValueError('Invalid model preset name.')
        if not isinstance(description, str) or len(description) > 300:
            raise ValueError('Invalid preset description.')
        data = _read_json(self.presets_file)
        if data.get('version') != 1 or not isinstance(data.get('presets'), dict):
            raise ValueError('Invalid preset catalog version.')
        if name in data['presets']:
            raise ValueError('Model preset already exists.')
        new_data = {**data, 'presets': {**data['presets'], name: {
            'description': description, 'stages': stages,
        }}}
        # Validate the complete model-to-stage contract before writing to disk.
        from .model_presets import resolve_preset
        resolve_preset(new_data, name, _read_json(self.models_file)['profiles'])
        _atomic_json(self.presets_file, new_data)
        return {'created': name}

    def _remote_stages(self, gateway):
        return [name for name, spec in (gateway.selected_profiles() or {}).items()
                if spec['endpoint'] not in ('ollama-local', 'local-compatible')]

    def prepare_activation(self, preset, *, allow_remote=False):
        pinned = os.getenv('ALIGO_MODEL_PRESET', '').strip()
        if pinned and preset != pinned:
            raise ValueError('Unset ALIGO_MODEL_PRESET before activating another model preset.')
        if preset is not None and (not isinstance(preset, str) or preset not in self.presets()):
            raise ValueError('Unknown model preset.')
        gateway = self.build_gateway(preset)
        remote = self._remote_stages(gateway)
        if remote and not allow_remote:
            raise PermissionError('Remote model stages require allow_remote=true.')
        if gateway.selected_profiles():
            missing = [stage for stage, profile in gateway._stage_profiles.items()
                       if not gateway._configured(profile)]
            if missing:
                raise ValueError('Unconfigured model credentials for stages: ' + ', '.join(missing))
        return gateway, remote

    def commit_activation(self, preset, gateway, remote):
        _atomic_json(self.state_file, {'preset': preset, 'remote_authorized': bool(remote)})
        self.gateway = gateway
        self.active_preset = preset
        self.remote_authorized = bool(remote)

    async def check_compatibility(self, preset, stages=None, *, connect=False,
                                  allow_remote=False):
        gateway, remote = self.prepare_activation(preset, allow_remote=allow_remote)
        selected = gateway.selected_profiles()
        if selected is None:
            return {'preset': None, 'status': 'legacy_unverified', 'stages': {}}
        requested = list(stages) if stages is not None else ['daily']
        if not requested or len(requested) > 6 or len(set(requested)) != len(requested):
            raise ValueError('Choose one to six unique planning stages.')
        if any(name not in selected for name in requested):
            raise ValueError('Unknown planning stage.')
        results = {}
        for stage in requested:
            record = {'profile': selected[stage]['profile'],
                      'model': selected[stage]['model'],
                      'endpoint': selected[stage]['endpoint'],
                      'status': 'configuration_valid'}
            if connect:
                role = 'reasoning' if stage in ('blueprint', 'daily', 'repair') else stage
                try:
                    # The smoke test is deliberately small and does not claim to
                    # validate the full itinerary schema or geographic accuracy.
                    response = await gateway.complete(
                        role, 'Return a JSON object with the boolean property ok.',
                        'Reply with exactly {"ok": true}.', json_mode=True,
                        max_output_tokens=96, stage=stage)
                    if response.get('ok') is True:
                        record['status'] = 'json_smoke_pass'
                    else:
                        record['status'] = 'json_smoke_fail'
                except ModelUnavailable:
                    record['status'] = 'request_failed'
            results[stage] = record
        return {'preset': preset, 'status': ('smoke_pass' if connect and
                all(v['status'] == 'json_smoke_pass' for v in results.values()) else
                'smoke_failed' if connect else 'configuration_valid'),
                'remote_stages': remote, 'stages': results,
                'limits': 'A small JSON smoke test does not certify full trip generation.'}
