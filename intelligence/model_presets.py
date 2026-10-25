"""Validated, opt-in model routing presets for the itinerary planning pipeline.

Presets refer to profiles in config/models.json. They never contain API keys
and never silently fall back to an unrelated provider or model.
"""
from __future__ import annotations

import json
from pathlib import Path

STAGES = ('router', 'reasoning', 'synthesis', 'blueprint', 'daily', 'repair')
STAGE_ROLE = {
    'router': 'router', 'reasoning': 'reasoning', 'synthesis': 'synthesis',
    'blueprint': 'reasoning', 'daily': 'reasoning', 'repair': 'reasoning',
}
OVERRIDES = {
    'model', 'temperature', 'max_output_tokens', 'max_completion_tokens',
    'request_timeout_seconds', 'reasoning_effort', 'disable_reasoning',
    'structured_output_mode',
}


def load_preset(path: Path, name: str, profiles: list[dict]) -> dict:
    """Return a validated, copy-safe stage routing map for one named preset."""
    content = json.loads(Path(path).read_text(encoding='utf-8'))
    return resolve_preset(content, name, profiles)


def resolve_preset(content: dict, name: str, profiles: list[dict]) -> dict:
    if not isinstance(content, dict) or content.get('version') != 1:
        raise ValueError('Model presets require version 1.')
    collection = content.get('presets')
    if not isinstance(collection, dict) or name not in collection:
        raise ValueError(f'Unknown model preset: {name}')
    value = collection[name]
    if not isinstance(value, dict) or not isinstance(value.get('stages'), dict):
        raise ValueError(f'Invalid stages for model preset: {name}')
    stages = value['stages']
    if set(stages) != set(STAGES):
        raise ValueError('Model presets must define all six planning stages.')
    index = {p.get('name'): p for p in profiles}
    resolved = {}
    for stage in STAGES:
        spec = stages[stage]
        if isinstance(spec, str):
            spec = {'profile': spec}
        if not isinstance(spec, dict) or not isinstance(spec.get('profile'), str):
            raise ValueError(f'Invalid {stage} model preset mapping.')
        if set(spec) - {'profile'} - OVERRIDES:
            raise ValueError(f'Unsupported {stage} model preset override.')
        original = index.get(spec['profile'])
        if original is None or original.get('role') != STAGE_ROLE[stage] or original.get('provider') != 'openai-compatible':
            raise ValueError(f'Invalid {stage} profile reference: {spec["profile"]}')
        changed = dict(original)
        if 'model' in spec:
            if not isinstance(spec['model'], str) or not spec['model'].strip():
                raise ValueError(f'Invalid model for stage {stage}.')
            # An explicit preset model must not be replaced by POREIAGO_LOCAL_MODEL.
            changed.pop('model_env', None)
        for key in set(spec) - {'profile'}:
            item = spec[key]
            if key in {'max_output_tokens', 'max_completion_tokens'}:
                if isinstance(item, bool) or not isinstance(item, int) or item <= 0:
                    raise ValueError(f'Invalid {key} at stage {stage}.')
            elif key == 'temperature':
                if isinstance(item, bool) or not isinstance(item, (int, float)) or not 0 <= item <= 2:
                    raise ValueError(f'Invalid temperature at stage {stage}.')
            elif key == 'request_timeout_seconds':
                if isinstance(item, bool) or not isinstance(item, (int, float)) or item <= 0:
                    raise ValueError(f'Invalid timeout at stage {stage}.')
            elif key == 'disable_reasoning' and not isinstance(item, bool):
                raise ValueError(f'Invalid disable_reasoning at stage {stage}.')
            elif key == 'structured_output_mode' and item not in {'auto', 'json_schema', 'json_object'}:
                raise ValueError(f'Invalid structured_output_mode at stage {stage}.')
            elif key == 'reasoning_effort' and not isinstance(item, str):
                raise ValueError(f'Invalid reasoning_effort at stage {stage}.')
            changed[key] = item
        # Do not allow both mutually exclusive token budget protocols.
        if 'max_output_tokens' in spec:
            changed.pop('max_completion_tokens', None)
        if 'max_completion_tokens' in spec:
            changed.pop('max_output_tokens', None)
        changed['_explicit_stage_max_tokens'] = ('max_output_tokens' in spec or 'max_completion_tokens' in spec)
        resolved[stage] = changed
    return resolved


def list_presets(path: Path) -> list[str]:
    content = json.loads(Path(path).read_text(encoding='utf-8'))
    if not isinstance(content, dict) or not isinstance(content.get('presets'), dict):
        raise ValueError('Invalid preset catalog.')
    return sorted(content['presets'])
