# multi-stop-route-optimizer: professional travel workflow

## Objective
Suggest efficient stop order by proximity. This Skill is independently executable through its local `handler.py` entrypoint.

## When to activate
Use when a traveler or itinerary validation specifically needs this capability. Do not invoke on every day simply because the Skill exists. If an answer can be obtained from already validated trip state, reuse that evidence.

## Inputs and prerequisites
Two to twenty coordinate-bearing stops, optionally return to start. Consult `manifest.json` for the machine-readable field types and `references/input_example.json` for a concrete invocation. Reject or mark partial when required information is missing.

## Execution protocol
1. Extract the relevant traveler constraints, location context, dates and prior confirmed decisions without silently assuming details.
2. Validate provided inputs and retain unambiguous identifiers, geographic coordinates or source metadata where available.
3. Use deterministic nearest-neighbor haversine and report straight-line legs.
4. Compare any alternatives against the actual itinerary state; avoid recommending already scheduled duplicates unless the user deliberately requested them.
5. Return structured output with `status`, `data`, `sources` and `limitations`. Prefer partial or unavailable to fabricated precision.
6. Keep original user state unchanged unless this Skill explicitly authorizes a persistent write.

## Output and downstream use
Return stop order, distance heuristic and limitations. Consumers must distinguish confirmed information, mapped candidates, supplied estimates and unknown facts. The planner may select a candidate, but must not turn candidate status into verified status.

## Quality gates
- Do not invent named venues, fares, operating hours, transfer times, accessible routes or reservation availability.
- Never infer source verification from the presence of a URL; compare the exact claim to retrieved evidence when a suitable provider exists.
- Enforce per-skill input bounds and deterministic order to prevent unbounded retries or misleading rankings.
- Preserve the correct location, currency, party size, date and itinerary day rather than copying unrelated context.
- Review duplication against trip registry when relevant and preserve the original itinerary for a repair workflow.

## Failure and recovery
Not a real transit or walking route; do not turn straight-line kms into ETA. On missing providers, return `unavailable` or `partial` with an explicit limitation. On ambiguous input, require disambiguation or return a set of alternatives; never guess a country, route or supplier.

## Example input
```json
{
  "waypoints": [
    {
      "latitude": 35.0,
      "longitude": 135.7
    },
    {
      "latitude": 35.1,
      "longitude": 135.8
    }
  ]
}
```

## Scope boundary
This Skill is a bounded specialist. Global day allocation remains with the Blueprint planner, and final acceptance remains with the Quality Gate. External real-time facts must come from providers with preserved provenance. No destination-specific travel facts are hard-coded in this Skill.

## V3.6.3 executable decision specification
1. Hold the first stop fixed; optionally fix the last stop or request a return-to-start cycle.
2. Build an initial nearest-neighbor distance proxy, then use bounded 2-opt subpath reversals to improve the metric.
3. Every reported distance is straight-line only; booking, walking, road and public-transit feasibility require external routes.

### Release acceptance scenario
Run this Skill with normal, ambiguous, malformed and missing-provider inputs. Assert truthful `status`, bounded outputs and preserved provenance. For complete travel tasks, compare the result with the original trip and revalidate any proposed local revision before accepting it.

## Itinerary-first maturity behavior
1. Require geographic coordinates for every planned visit, preserving first stop and fixed last stop.
2. Build nearest-neighbor order, then bounded 2-opt improvement without changing fixed endpoints.
3. Return individual straight-line legs and total proximity distance.
4. Flag outsized legs, total distance limit failures and switches between named areas.
5. Do not infer walking accessibility, public transport frequency or venue opening hours from Haversine distance.

### Quality and evidence boundary
Coordinates cannot prove a physically walkable route. Emit route proximity only.
