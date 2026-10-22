# trip-budget-allocation — Operational acceptance guide

## Specialist decision rules
1. Reserve a contingency category unless the traveler explicitly chooses another split.
2. Validate budget weights sum to exactly 100 percent within rounding tolerance.
3. Reconcile rounding residuals so category allocations sum back to the original trip budget.
4. Treat budget envelopes as planning limits, not a claim that hotels and tickets can be booked for those prices.

## Invocation and handoff
The orchestrator selects this specialist only for a matching traveler need. The Skill must receive explicit inputs and return a structured `SkillOutput`. A downstream planner can use candidate records as suggestions, but must retain source status, limitations and all known unknowns.

## Repair policy
When a quality audit flags a defect in this domain, first re-run the Skill using corrected input or additional trustworthy evidence. Submit only the affected activity or metadata field to the repair controller. Reject changes that create a new cross-day duplicate, timeline overlap, unsupported price or venue assertion.

## Minimum acceptance
An accepted result must be schema-compatible, bounded in size, preserve original geography and currency context, and not claim independent fact verification unless evidence was actually checked. Missing external data yields `partial` or `unavailable` rather than confident travel directions.

## V3.6.3 executable decision specification
1. Validate a positive trip budget, traveler count, days, currency and weights summing to 100 percent.
2. Support group_total and per_person input bases, preserving the distinction in reporting.
3. Budget envelopes are allocations rather than forecasts of hotel, ticket, or food market prices.

### Release acceptance scenario
Run this Skill with normal, ambiguous, malformed and missing-provider inputs. Assert truthful `status`, bounded outputs and preserved provenance. For complete travel tasks, compare the result with the original trip and revalidate any proposed local revision before accepting it.

## Itinerary-first advanced decision rules
1. Validate positive amount, person count, days, currency and category percentage totals.
2. Preserve per-person versus group basis without implicitly multiplying unrelated subtotals.
3. Allocate category envelopes with deterministic rounding correction.
4. Allocate the on-site categories across itinerary days using explicit day weights when supplied.
5. Label all outputs as planning budgets, not researched destination expenses or booking prices.

### Explicit failure boundary
Reject impossible weight sums and invalid daily weights instead of silently normalizing user inputs.

### Verification checklist
- Inspect output data, ranking reasons, ambiguity and provenance.
- Run normal, malformed, repeated and unavailable-source examples.
- Keep generated itinerary text and established reservations untouched unless downstream validation accepts a change.
