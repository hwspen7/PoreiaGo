# schedule-conflict-validation: professional travel workflow

## Objective
Detect event overlap and insufficient transfer buffers. This Skill is independently executable through its local `handler.py` entrypoint.

## When to activate
Use when a traveler or itinerary validation specifically needs this capability. Do not invoke on every day simply because the Skill exists. If an answer can be obtained from already validated trip state, reuse that evidence.

## Inputs and prerequisites
Time-ordered activity events with ISO timestamps, optional transfer buffer. Consult `manifest.json` for the machine-readable field types and `references/input_example.json` for a concrete invocation. Reject or mark partial when required information is missing.

## Execution protocol
1. Extract the relevant traveler constraints, location context, dates and prior confirmed decisions without silently assuming details.
2. Validate provided inputs and retain unambiguous identifiers, geographic coordinates or source metadata where available.
3. Normalize dates, validate intervals, sort, test overlap and dwell-to-transfer gaps.
4. Compare any alternatives against the actual itinerary state; avoid recommending already scheduled duplicates unless the user deliberately requested them.
5. Return structured output with `status`, `data`, `sources` and `limitations`. Prefer partial or unavailable to fabricated precision.
6. Keep original user state unchanged unless this Skill explicitly authorizes a persistent write.

## Output and downstream use
Return pairwise conflicts and malformed-event indices. Consumers must distinguish confirmed information, mapped candidates, supplied estimates and unknown facts. The planner may select a candidate, but must not turn candidate status into verified status.

## Quality gates
- Do not invent named venues, fares, operating hours, transfer times, accessible routes or reservation availability.
- Never infer source verification from the presence of a URL; compare the exact claim to retrieved evidence when a suitable provider exists.
- Enforce per-skill input bounds and deterministic order to prevent unbounded retries or misleading rankings.
- Preserve the correct location, currency, party size, date and itinerary day rather than copying unrelated context.
- Review duplication against trip registry when relevant and preserve the original itinerary for a repair workflow.

## Failure and recovery
A clean chronology does not prove attractions are actually reachable. On missing providers, return `unavailable` or `partial` with an explicit limitation. On ambiguous input, require disambiguation or return a set of alternatives; never guess a country, route or supplier.

## Example input
```json
{
  "events": [
    {
      "name": "Museum",
      "start": "2026-11-01T10:00",
      "end": "2026-11-01T11:00"
    },
    {
      "name": "Lunch",
      "start": "2026-11-01T11:30",
      "end": "2026-11-01T12:30"
    }
  ]
}
```

## Scope boundary
This Skill is a bounded specialist. Global day allocation remains with the Blueprint planner, and final acceptance remains with the Quality Gate. External real-time facts must come from providers with preserved provenance. No destination-specific travel facts are hard-coded in this Skill.

## V3.6.3 executable decision specification
1. Parse all event ISO datetime ranges; reject end-before-start, invalid buffers and mixed timezone interpretation.
2. Compare every interval pair for intersections so nested long/short activities are all detected.
3. Apply explicit transfer buffers to successive activities; geographic travel time needs routing evidence.

### Release acceptance scenario
Run this Skill with normal, ambiguous, malformed and missing-provider inputs. Assert truthful `status`, bounded outputs and preserved provenance. For complete travel tasks, compare the result with the original trip and revalidate any proposed local revision before accepting it.

## Itinerary-first maturity behavior
1. Parse all ISO timestamps and reject negative or missing-duration events.
2. Check every overlapping interval pair, including a long visit covering two short ones.
3. For consecutive events, require the maximum of the previous outgoing transfer buffer and the next incoming transfer+safety buffer.
4. Report overlap and insufficient transfer buffer separately, with exact index and shortfall.
5. Reject mixed aware/naive time bases and surface invalid events rather than silently accepting them.

### Quality and evidence boundary
A timetable conflict must never be hidden because an optional specialist is unavailable; core planning guards remain authoritative.
