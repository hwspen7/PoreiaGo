"""Trace model configuration and request/JSON compatibility without saving a journey.

Use --probe explicitly to send synthetic, non-sensitive model requests. The
output contains profile names, resolved model IDs and failure categories, but
never prints API keys, user messages or private prompts.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
from urllib.parse import urlsplit

from core.config import settings
from intelligence.model_management import ModelControl
from intelligence.models import ModelGateway
from core.runtime import safe_model_issue

STAGE_ROLE = {'router': 'router', 'reasoning': 'reasoning', 'synthesis': 'synthesis',
              'blueprint': 'reasoning', 'daily': 'reasoning', 'repair': 'reasoning'}
SCHEMA = {'type': 'object', 'properties': {'ok': {'type': 'boolean'}},
          'required': ['ok'], 'additionalProperties': False}


def stage_candidates(gateway, stage: str):
    if gateway._stage_profiles is not None:
        return [gateway._stage_profiles[stage]]
    return [profile for profile in gateway.profiles
            if profile.get('role') == STAGE_ROLE[stage] and
            profile.get('provider') == 'openai-compatible']


def public_profile(gateway, profile):
    endpoint = urlsplit(profile.get('base_url', ''))
    return {'profile': profile['name'],
            'model': __import__('os').getenv(profile.get('model_env', ''), profile.get('model', '')),
            'endpoint': f'{endpoint.scheme}://{endpoint.hostname}:{endpoint.port}'
                        if endpoint.port is not None else f'{endpoint.scheme}://{endpoint.hostname}',
            'credential_configured': gateway._configured(profile)}


async def diagnose(*, probe: bool, stages: list[str], timeout: float,
                   all_configured: bool, native_schema: bool, max_tokens: int) -> dict:
    control = ModelControl(settings)
    gateway = control.gateway
    report = {'preset': control.active_preset or 'legacy', 'mode': 'probe' if probe else 'configuration',
              'stages': [], 'status': 'PASS'}
    for stage in stages:
        candidates = stage_candidates(gateway, stage)
        if not all_configured and gateway._stage_profiles is None:
            candidates = candidates[:1]
        for profile in candidates:
            summary = {'stage': stage, **public_profile(gateway, profile)}
            if not summary['credential_configured']:
                summary['status'] = 'NOT_CONFIGURED'
            elif not probe:
                summary['status'] = 'CONFIGURED_NOT_PROBED'
            else:
                # Force exactly this one profile. A successful backup must not
                # mask a broken primary profile in diagnostic mode.
                isolated = ModelGateway(settings)
                isolated._stage_profiles = {stage: dict(profile)}
                isolated._clients = {}
                # The per-request deadline is independent of the source profile's
                # configured slow-generation timeout.
                isolated._stage_profiles[stage]['request_timeout_seconds'] = timeout
                start = time.perf_counter()
                try:
                    output = await asyncio.wait_for(isolated.complete(
                        STAGE_ROLE[stage],
                        'Reply with exactly the JSON object {"ok":true}. No other text.',
                        'Test the JSON-compatible model response; do not answer a travel question.',
                        json_mode=True,
                        response_schema=SCHEMA if native_schema else None,
                        max_output_tokens=max_tokens, stage=stage), timeout=timeout + 5)
                    summary['elapsed_seconds'] = round(time.perf_counter() - start, 2)
                    summary['status'] = 'PASS' if output.get('ok') is True else 'INVALID_CONTENT'
                    if isolated.completion_metadata():
                        info = isolated.completion_metadata()
                        summary['finish_reason'] = info.get('finish_reason')
                        summary['schema_fallback'] = info.get('schema_fallback', False)
                except Exception as exc:
                    summary['elapsed_seconds'] = round(time.perf_counter() - start, 2)
                    summary['status'] = safe_model_issue(exc)
                    if summary['status'] == 'MODEL_UNAVAILABLE':
                        # ModelGateway exceptions contain sanitized HTTP categories,
                        # not raw provider messages. Avoid echoing arbitrary content.
                        summary['status'] = 'MODEL_UNAVAILABLE_CHECK_BACKEND_LOG'
            if summary['status'] not in ('PASS', 'CONFIGURED_NOT_PROBED'):
                report['status'] = 'FAILED' if probe else 'INCOMPLETE'
            report['stages'].append(summary)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--probe', action='store_true', help='Actually send synthetic JSON requests to models.')
    parser.add_argument('--stages', default='router,reasoning', help='Comma-separated model stages to inspect.')
    parser.add_argument('--timeout', type=float, default=90.0, help='Per-request deadline in seconds.')
    parser.add_argument('--all-configured', action='store_true', help='Inspect every legacy profile rather than only the first.')
    parser.add_argument('--native-schema', action='store_true', help='Probe strict JSON-schema request compatibility.')
    parser.add_argument('--max-tokens', type=int, default=2048, help='Probe output-token ceiling (default 2048).')
    args = parser.parse_args()
    stages = [s.strip() for s in args.stages.split(',') if s.strip()]
    if not stages or any(s not in STAGE_ROLE for s in stages):
        parser.error('Stages must be drawn from: ' + ', '.join(STAGE_ROLE))
    if not 1 <= args.timeout <= 3600:
        parser.error('The timeout must be between 1 and 3600 seconds.')
    if not 96 <= args.max_tokens <= 16384:
        parser.error('The probe max-tokens value must be between 96 and 16384.')
    result = asyncio.run(diagnose(probe=args.probe, stages=stages,
                                  timeout=args.timeout, all_configured=args.all_configured,
                                  native_schema=args.native_schema, max_tokens=args.max_tokens))
    print(json.dumps(result, indent=2))
    if args.probe and result['status'] != 'PASS':
        raise SystemExit(1)


if __name__ == '__main__':
    main()
