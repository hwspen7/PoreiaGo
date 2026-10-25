# itinerary-feasibility-audit: Professional itinerary workflow

## Objective
Non-destructively flag itinerary overlaps, repeated place identifiers, and unverified activities.

## When to activate
Invoke this Skill only when the itinerary or traveler requirements make its specialty relevant. Its availability in the catalog does not require calling it for every trip or day.

## Inputs and prerequisites
The minimal input contract is shown in `references/input_example.json`. Inputs must contain final daily activity list. Only supplied or provider-sourced candidate data may be used as factual evidence.

## Execution protocol
1. Receive structured trip details and applicable preferences from the itinerary orchestrator.
2. Validate candidate types and enforce limits from `manifest.json`.
3. Preserve supplied place or event identity and provenance where available.
4. Apply relevance, timing or constraint filtering specific to this Skill.
5. Return bounded structured `data` with a status and explicit evidence limitations.
6. Report unknown details rather than guessing them or silently excluding entire cities.
7. Keep the output advisory: the selected LLM designs the traveler-facing experience.

## Output contract
The Skill returns `SkillOutput` with `status`, `text`, `data`, `sources`, and `limitations`. A candidate-level result is not a verified booking, a guaranteed admission, a live road route, or an authoritative safety rating.

## Independent execution
Call `SkillRegistry.run('itinerary-feasibility-audit', ctx, arguments)` with the JSON input example. The registry imports `handler.py:run` on demand and validates the input schema before execution.

## Reliability and sourcing
- Do not invent attractions, events, menus, supplier schedules, opening times, accessibility guarantees or transport durations.
- Treat missing source fields as unknown and keep supplier or traveler-provided assumptions explicitly unverified.
- Never commit changes to a saved itinerary from this Skill.
- Avoid free-form model completions in the Skill handler; the model owns itinerary design.
- Keep bounded result counts to protect small local model context windows.

## Itinerary-first maturity behavior
This Skill performs real input-specific work and returns concrete diagnostics, bounded candidate records, or both. It is safe to execute offline on supplied examples and integrates with the existing model-neutral planning pipeline.

## Input provenance
All example venues are synthetic fixture data. No external provider is assumed.

## Performance policy
No hidden network calls are made from this handler; it processes bounded records only.

## Failure behavior
Invalid inputs return `partial` or schema errors. Missing evidence does not trigger fabrication. See `references/OPERATIONS.md` for integration limits.
