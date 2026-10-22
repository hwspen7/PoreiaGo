# trip-budget-allocation: professional travel workflow

## Objective
Allocate a realistic envelope from user-specified budget. This Skill is independently executable through its local `handler.py` entrypoint.

## When to activate
Use when a traveler or itinerary validation specifically needs this capability. Do not invoke on every day simply because the Skill exists. If an answer can be obtained from already validated trip state, reuse that evidence.

## Inputs and prerequisites
Total budget, currency, days, travelers, optional category weights. Consult `manifest.json` for the machine-readable field types and `references/input_example.json` for a concrete invocation. Reject or mark partial when required information is missing.

## Execution protocol
1. Extract the relevant traveler constraints, location context, dates and prior confirmed decisions without silently assuming details.
2. Validate provided inputs and retain unambiguous identifiers, geographic coordinates or source metadata where available.
3. Validate 100-percent weights, allocate exact total and contingency.
4. Compare any alternatives against the actual itinerary state; avoid recommending already scheduled duplicates unless the user deliberately requested them.
5. Return structured output with `status`, `data`, `sources` and `limitations`. Prefer partial or unavailable to fabricated precision.
6. Keep original user state unchanged unless this Skill explicitly authorizes a persistent write.

## Output and downstream use
Return per-day, per-traveler, and category spending ceilings. Consumers must distinguish confirmed information, mapped candidates, supplied estimates and unknown facts. The planner may select a candidate, but must not turn candidate status into verified status.

## Quality gates
- Do not invent named venues, fares, operating hours, transfer times, accessible routes or reservation availability.
- Never infer source verification from the presence of a URL; compare the exact claim to retrieved evidence when a suitable provider exists.
- Enforce per-skill input bounds and deterministic order to prevent unbounded retries or misleading rankings.
- Preserve the correct location, currency, party size, date and itinerary day rather than copying unrelated context.
- Review duplication against trip registry when relevant and preserve the original itinerary for a repair workflow.

## Failure and recovery
This is a budget allocation, not a sourced market price quote. On missing providers, return `unavailable` or `partial` with an explicit limitation. On ambiguous input, require disambiguation or return a set of alternatives; never guess a country, route or supplier.

## Example input
```json
{
  "total": 1200,
  "currency": "USD",
  "days": 4,
  "travelers": 2
}
```

## Scope boundary
This Skill is a bounded specialist. Global day allocation remains with the Blueprint planner, and final acceptance remains with the Quality Gate. External real-time facts must come from providers with preserved provenance. No destination-specific travel facts are hard-coded in this Skill.

## V3.6.3 executable decision specification
1. Validate a positive trip budget, traveler count, days, currency and weights summing to 100 percent.
2. Support group_total and per_person input bases, preserving the distinction in reporting.
3. Budget envelopes are allocations rather than forecasts of hotel, ticket, or food market prices.

### Release acceptance scenario
Run this Skill with normal, ambiguous, malformed and missing-provider inputs. Assert truthful `status`, bounded outputs and preserved provenance. For complete travel tasks, compare the result with the original trip and revalidate any proposed local revision before accepting it.

## Itinerary-first maturity behavior
1. Validate positive amount, person count, days, currency and category percentage totals.
2. Preserve per-person versus group basis without implicitly multiplying unrelated subtotals.
3. Allocate category envelopes with deterministic rounding correction.
4. Allocate the on-site categories across itinerary days using explicit day weights when supplied.
5. Label all outputs as planning budgets, not researched destination expenses or booking prices.

### Quality and evidence boundary
Reject impossible weight sums and invalid daily weights instead of silently normalizing user inputs.
