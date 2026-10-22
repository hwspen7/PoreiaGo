# hourly-weather-forecast — Operational acceptance guide

## Specialist decision rules
1. Use timezone-labelled provider timestamps rather than model-generated clock times.
2. Limit forecast horizons to the supported provider range; distant plans use seasonal caveats instead.
3. Forecast probability must be reported as a probability, not a guarantee of precipitation.
4. Official extreme-weather alerts should be checked outside this basic hourly forecast.

## Invocation and handoff
The orchestrator selects this specialist only for a matching traveler need. The Skill must receive explicit inputs and return a structured `SkillOutput`. A downstream planner can use candidate records as suggestions, but must retain source status, limitations and all known unknowns.

## Repair policy
When a quality audit flags a defect in this domain, first re-run the Skill using corrected input or additional trustworthy evidence. Submit only the affected activity or metadata field to the repair controller. Reject changes that create a new cross-day duplicate, timeline overlap, unsupported price or venue assertion.

## Minimum acceptance
An accepted result must be schema-compatible, bounded in size, preserve original geography and currency context, and not claim independent fact verification unless evidence was actually checked. Missing external data yields `partial` or `unavailable` rather than confident travel directions.

## V3.6.3 executable decision specification
1. Resolve one location, request no more than fourteen forecast days, read only valid provider arrays.
2. Expose timestamped high-rain-risk periods from forecasted precipitation probability >=65%; tolerate missing weather values.
3. Do not extrapolate weather beyond forecast horizon or imply a substitute indoor venue is open.

### Release acceptance scenario
Run this Skill with normal, ambiguous, malformed and missing-provider inputs. Assert truthful `status`, bounded outputs and preserved provenance. For complete travel tasks, compare the result with the original trip and revalidate any proposed local revision before accepting it.

## Itinerary-first advanced decision rules
1. Resolve a uniquely identified city or valid coordinates.
2. Retrieve an actual provider forecast and retain source evidence.
3. Filter hourly data to the explicitly requested local date when supplied.
4. Flag precipitation probabilities above the requested threshold; coalesce only adjacent forecast hours.
5. Inform the planner that outdoor stops need review, but do not invent open indoor alternatives.

### Explicit failure boundary
A requested date absent from the forecast is unknown, not sunny.

### Verification checklist
- Inspect output data, ranking reasons, ambiguity and provenance.
- Run normal, malformed, repeated and unavailable-source examples.
- Keep generated itinerary text and established reservations untouched unless downstream validation accepts a change.
