# place-detail-verification: professional travel workflow

## Objective
Resolve candidate identity cautiously. This Skill is independently executable through its local `handler.py` entrypoint.

## When to activate
Use when a traveler or itinerary validation specifically needs this capability. Do not invoke on every day simply because the Skill exists. If an answer can be obtained from already validated trip state, reuse that evidence.

## Inputs and prerequisites
A place name, optionally a city, and trusted candidate records. Consult `manifest.json` for the machine-readable field types and `references/input_example.json` for a concrete invocation. Reject or mark partial when required information is missing.

## Execution protocol
1. Extract the relevant traveler constraints, location context, dates and prior confirmed decisions without silently assuming details.
2. Validate provided inputs and retain unambiguous identifiers, geographic coordinates or source metadata where available.
3. Match exact normalized canonical name or declared aliases; reject ambiguous outcomes.
4. Compare any alternatives against the actual itinerary state; avoid recommending already scheduled duplicates unless the user deliberately requested them.
5. Return structured output with `status`, `data`, `sources` and `limitations`. Prefer partial or unavailable to fabricated precision.
6. Keep original user state unchanged unless this Skill explicitly authorizes a persistent write.

## Output and downstream use
Return matched candidate records and conservative verification level. Consumers must distinguish confirmed information, mapped candidates, supplied estimates and unknown facts. The planner may select a candidate, but must not turn candidate status into verified status.

## Optional provider detail enrichment
If a genuine Geoapify `place_id` is supplied and the `place_details` provider is enabled, retrieve its current detail record. Keep this as provider evidence, never as a guarantee of current opening hours or a confirmed booking. This mode is entirely optional and must degrade cleanly when the provider is not configured.

## Quality gates
- Do not invent named venues, fares, operating hours, transfer times, accessible routes or reservation availability.
- Never infer source verification from the presence of a URL; compare the exact claim to retrieved evidence when a suitable provider exists.
- Enforce per-skill input bounds and deterministic order to prevent unbounded retries or misleading rankings.
- Preserve the correct location, currency, party size, date and itinerary day rather than copying unrelated context.
- Review duplication against trip registry when relevant and preserve the original itinerary for a repair workflow.

## Failure and recovery
Candidate absence is not evidence that a place does not exist. On missing providers, return `unavailable` or `partial` with an explicit limitation. On ambiguous input, require disambiguation or return a set of alternatives; never guess a country, route or supplier.

## Example input
```json
{
  "place": "Example Museum",
  "candidates": [
    {
      "name": "Example Museum",
      "osm_id": 8
    }
  ]
}
```

## Scope boundary
This Skill is a bounded specialist. Global day allocation remains with the Blueprint planner, and final acceptance remains with the Quality Gate. External real-time facts must come from providers with preserved provenance. No destination-specific travel facts are hard-coded in this Skill.

## V3.6.3 executable decision specification
1. Match explicit place identifiers or exact names/aliases, then narrow by known city or country.
2. Multiple distinct candidates are ambiguous and cannot be promoted to a single confirmed identity.
3. Even an API place-detail response cannot itself verify current hours, admission, bookable sessions or accessibility.

### Release acceptance scenario
Run this Skill with normal, ambiguous, malformed and missing-provider inputs. Assert truthful `status`, bounded outputs and preserved provenance. For complete travel tasks, compare the result with the original trip and revalidate any proposed local revision before accepting it.

## Itinerary-first maturity behavior
1. Use exact aliases or a stable place_id, never substring-based place identity.
2. If place_id is supplied, reject candidates with another ID even when the name matches.
3. Apply city and country restrictions against explicit fields first, then address if fields are absent.
4. Match provider detail results against the requested place_id and geographic scope.
5. Return an ambiguous/unknown status rather than turning candidate matching into factual verification.

### Quality and evidence boundary
A missing candidate is unknown, not proof a place has closed or does not exist.
