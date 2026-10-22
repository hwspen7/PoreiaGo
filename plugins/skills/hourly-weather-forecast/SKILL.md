# hourly-weather-forecast: professional travel workflow

## Objective
Retrieve location-specific hourly forecast. This Skill is independently executable through its local `handler.py` entrypoint.

## When to activate
Use when a traveler or itinerary validation specifically needs this capability. Do not invoke on every day simply because the Skill exists. If an answer can be obtained from already validated trip state, reuse that evidence.

## Inputs and prerequisites
Location/coordinates and bounded forecast days. Consult `manifest.json` for the machine-readable field types and `references/input_example.json` for a concrete invocation. Reject or mark partial when required information is missing.

## Execution protocol
1. Extract the relevant traveler constraints, location context, dates and prior confirmed decisions without silently assuming details.
2. Validate provided inputs and retain unambiguous identifiers, geographic coordinates or source metadata where available.
3. Geocode if needed, request weather provider, produce short hourly preview.
4. Compare any alternatives against the actual itinerary state; avoid recommending already scheduled duplicates unless the user deliberately requested them.
5. Return structured output with `status`, `data`, `sources` and `limitations`. Prefer partial or unavailable to fabricated precision.
6. Keep original user state unchanged unless this Skill explicitly authorizes a persistent write.

## Output and downstream use
Return forecast with provider timezone and timestamps. Consumers must distinguish confirmed information, mapped candidates, supplied estimates and unknown facts. The planner may select a candidate, but must not turn candidate status into verified status.

## Quality gates
- Do not invent named venues, fares, operating hours, transfer times, accessible routes or reservation availability.
- Never infer source verification from the presence of a URL; compare the exact claim to retrieved evidence when a suitable provider exists.
- Enforce per-skill input bounds and deterministic order to prevent unbounded retries or misleading rankings.
- Preserve the correct location, currency, party size, date and itinerary day rather than copying unrelated context.
- Review duplication against trip registry when relevant and preserve the original itinerary for a repair workflow.

## Failure and recovery
Forecast beyond supported horizon and severe weather alerts require other services. On missing providers, return `unavailable` or `partial` with an explicit limitation. On ambiguous input, require disambiguation or return a set of alternatives; never guess a country, route or supplier.

## Example input
```json
{
  "city": "Kyoto",
  "forecast_days": 3
}
```

## Scope boundary
This Skill is a bounded specialist. Global day allocation remains with the Blueprint planner, and final acceptance remains with the Quality Gate. External real-time facts must come from providers with preserved provenance. No destination-specific travel facts are hard-coded in this Skill.

## V3.6.3 executable decision specification
1. Resolve one location, request no more than fourteen forecast days, read only valid provider arrays.
2. Expose timestamped high-rain-risk periods from forecasted precipitation probability >=65%; tolerate missing weather values.
3. Do not extrapolate weather beyond forecast horizon or imply a substitute indoor venue is open.

### Release acceptance scenario
Run this Skill with normal, ambiguous, malformed and missing-provider inputs. Assert truthful `status`, bounded outputs and preserved provenance. For complete travel tasks, compare the result with the original trip and revalidate any proposed local revision before accepting it.

## Itinerary-first maturity behavior
1. Resolve a uniquely identified city or valid coordinates.
2. Retrieve an actual provider forecast and retain source evidence.
3. Filter hourly data to the explicitly requested local date when supplied.
4. Flag precipitation probabilities above the requested threshold; coalesce only adjacent forecast hours.
5. Inform the planner that outdoor stops need review, but do not invent open indoor alternatives.

### Quality and evidence boundary
A requested date absent from the forecast is unknown, not sunny.
