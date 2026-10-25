"""Safe, opt-in live diagnostics for the travel planner's structured output."""
from __future__ import annotations

import argparse
import asyncio

from core.config import settings
from intelligence.model_agent import model_first_plan
from intelligence.models import ModelGateway, ModelUnavailable
from services.registry import SkillRegistry
from core.runtime import Runtime, extract_fields
from planning.trip_state import TripValidationError


async def diagnose(request: str | None = None) -> int:
    models = ModelGateway(settings)
    registry = SkillRegistry(settings.extensions_dir)
    if not models.configured('router'):
        print('FAILED: router model credentials are not configured.')
        return 1
    # Verify the installed router profile, not merely the installer source.
    # Avoid spending local inference time if registration was skipped.
    profiles = getattr(models, 'profiles', None)
    if isinstance(profiles, list):
        configured = getattr(models, '_configured', lambda profile: False)
        router = next((p for p in profiles
                       if p.get('role') == 'router'
                       and p.get('provider') == 'openai-compatible'
                       and configured(p)), None)
        if router and router.get('name') == 'ollama-router':
            print('Router profile:', router['name'])
            print('Router decision-only:', router.get('decision_only') is True)
            print('Router output tokens:', router.get('max_output_tokens'))
            print('Router reasoning effort:', router.get('reasoning_effort', 'default'))
            if router.get('decision_only') is not True or router.get('reasoning_effort') != 'none':
                print('FAILED: stale Ollama router profile; run python -m models.configure_local_llm')
                return 1
    request = request or 'Design a relaxed five-day trip in Japan with scenery, culture, and local food.'
    fields = extract_fields(request)
    try:
        frame, answer = await model_first_plan(
            request, [], fields, {}, models, registry, settings.max_skill_calls)
    except (ModelUnavailable, ValueError, TypeError, KeyError) as exc:
        from core.runtime import safe_model_issue
        print('FAILED: router request:', safe_model_issue(exc))
        return 1
    print('Planner mode:', frame.mode)
    print('Task operation:', frame.task.get('operation'))
    print('Selected Skill count:', len(frame.skill_calls))
    if frame.task.get('operation') == 'create':
        try:
            # Exercise the same validation-and-repair path as the live Runtime.
            # No MemoryStore is created, and no itinerary is persisted.
            runtime = Runtime(None, models, None, registry, settings, retrieval=object())
            itinerary, _, _ = await runtime._prepare_trip_update(
                request, [], None, '', frame, answer, [], {}, frame.fields)
        except TripValidationError as exc:
            print('FAILED: structured itinerary:', str(exc))
            print('No database changes were made.')
            return 2
        print('PASSED: structured itinerary with', len(itinerary['days']), 'day(s).')
        print('Destination:', itinerary['destination'])
        print('No database changes were made.')
        return 0
    print('No structured itinerary was returned; no database changes were made.')
    return 2


def main():
    parser = argparse.ArgumentParser(description='Test the live model without saving trip data.')
    parser.add_argument('--live', action='store_true', help='Authorize a real API request that may use quota.')
    parser.add_argument('--request', type=str, help='Optional planning request to validate.')
    args = parser.parse_args()
    if not args.live:
        parser.error('Specify --live to authorize a cloud model request.')
    raise SystemExit(asyncio.run(diagnose(args.request)))


if __name__ == '__main__':
    main()
