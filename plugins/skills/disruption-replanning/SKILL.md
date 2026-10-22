# disruption-replanning: professional travel workflow

## Objective
Produce non-destructive alternatives when plans change. This Skill is independently executable through its local `handler.py` entrypoint.

## When to activate
Use when a traveler or itinerary validation specifically needs this capability. Do not invoke on every day simply because the Skill exists. If an answer can be obtained from already validated trip state, reuse that evidence.

## Inputs and prerequisites
Current day activities and disruption descriptor with affected places. Consult `manifest.json` for the machine-readable field types and `references/input_example.json` for a concrete invocation. Reject or mark partial when required information is missing.

## Execution protocol
1. Extract the relevant traveler constraints, location context, dates and prior confirmed decisions without silently assuming details.
2. Validate provided inputs and retain unambiguous identifiers, geographic coordinates or source metadata where available.
3. Mark affected activities; propose time shifts for delayed itineraries; require acceptance.
4. Compare any alternatives against the actual itinerary state; avoid recommending already scheduled duplicates unless the user deliberately requested them.
5. Return structured output with `status`, `data`, `sources` and `limitations`. Prefer partial or unavailable to fabricated precision.
6. Keep original user state unchanged unless this Skill explicitly authorizes a persistent write.

## Output and downstream use
Return a patch proposal, impacted positions and approval requirement. Consumers must distinguish confirmed information, mapped candidates, supplied estimates and unknown facts. The planner may select a candidate, but must not turn candidate status into verified status.

## Quality gates
- Do not invent named venues, fares, operating hours, transfer times, accessible routes or reservation availability.
- Never infer source verification from the presence of a URL; compare the exact claim to retrieved evidence when a suitable provider exists.
- Enforce per-skill input bounds and deterministic order to prevent unbounded retries or misleading rankings.
- Preserve the correct location, currency, party size, date and itinerary day rather than copying unrelated context.
- Review duplication against trip registry when relevant and preserve the original itinerary for a repair workflow.

## Failure and recovery
Do not cancel bookings or overwrite original itinerary without approval. On missing providers, return `unavailable` or `partial` with an explicit limitation. On ambiguous input, require disambiguation or return a set of alternatives; never guess a country, route or supplier.

## Example input
```json
{
  "activities": [
    {
      "place": "Museum",
      "start_time": "10:00"
    }
  ],
  "disruption": {
    "type": "closure",
    "affected_places": [
      "Museum"
    ]
  }
}
```

## Scope boundary
This Skill is a bounded specialist. Global day allocation remains with the Blueprint planner, and final acceptance remains with the Quality Gate. External real-time facts must come from providers with preserved provenance. No destination-specific travel facts are hard-coded in this Skill.

## V3.6.3 executable decision specification
1. Classify the reported disruption, locate the affected activity indices and shift only the affected suffix if a delay is given.
2. Return a non-mutating proposal with the changed indices and proposed start times; preserve the original input.
3. Require the user to approve a modification and re-run schedule and booking checks after adoption.

### Release acceptance scenario
Run this Skill with normal, ambiguous, malformed and missing-provider inputs. Assert truthful `status`, bounded outputs and preserved provenance. For complete travel tasks, compare the result with the original trip and revalidate any proposed local revision before accepting it.

## Itinerary-first maturity behavior
1. Work on a copy of the itinerary and preserve untouched activities.
2. Identify affected places by explicit disruption scope and use only supplied alternative names.
3. Exclude previously visited and already scheduled alternatives.
4. Shift only relevant flexible activities; flag confirmed bookings/fixed-time reservations rather than silently moving them.
5. Return proposed replacements and require approval plus final schedule verification before commitment.

### Quality and evidence boundary
Do not claim a replacement is open or booked; provide an approval-required proposal.
