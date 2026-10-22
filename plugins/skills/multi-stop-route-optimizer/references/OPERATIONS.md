# multi-stop-route-optimizer — Operational acceptance guide

## Specialist decision rules
1. Keep the traveler-requested first stop as the starting point.
2. Use nearest-neighbor haversine solely to suggest a rough visit order.
3. Avoid presenting geographic proximity as proof of equal terrain, transit or walking accessibility.
4. For constrained operating hours or reservations, demand a separate temporal feasibility check.

## Invocation and handoff
The orchestrator selects this specialist only for a matching traveler need. The Skill must receive explicit inputs and return a structured `SkillOutput`. A downstream planner can use candidate records as suggestions, but must retain source status, limitations and all known unknowns.

## Repair policy
When a quality audit flags a defect in this domain, first re-run the Skill using corrected input or additional trustworthy evidence. Submit only the affected activity or metadata field to the repair controller. Reject changes that create a new cross-day duplicate, timeline overlap, unsupported price or venue assertion.

## Minimum acceptance
An accepted result must be schema-compatible, bounded in size, preserve original geography and currency context, and not claim independent fact verification unless evidence was actually checked. Missing external data yields `partial` or `unavailable` rather than confident travel directions.

## V3.6.3 executable decision specification
1. Hold the first stop fixed; optionally fix the last stop or request a return-to-start cycle.
2. Build an initial nearest-neighbor distance proxy, then use bounded 2-opt subpath reversals to improve the metric.
3. Every reported distance is straight-line only; booking, walking, road and public-transit feasibility require external routes.

### Release acceptance scenario
Run this Skill with normal, ambiguous, malformed and missing-provider inputs. Assert truthful `status`, bounded outputs and preserved provenance. For complete travel tasks, compare the result with the original trip and revalidate any proposed local revision before accepting it.

## Itinerary-first advanced decision rules
1. Require geographic coordinates for every planned visit, preserving first stop and fixed last stop.
2. Build nearest-neighbor order, then bounded 2-opt improvement without changing fixed endpoints.
3. Return individual straight-line legs and total proximity distance.
4. Flag outsized legs, total distance limit failures and switches between named areas.
5. Do not infer walking accessibility, public transport frequency or venue opening hours from Haversine distance.

### Explicit failure boundary
Coordinates cannot prove a physically walkable route. Emit route proximity only.

### Verification checklist
- Inspect output data, ranking reasons, ambiguity and provenance.
- Run normal, malformed, repeated and unavailable-source examples.
- Keep generated itinerary text and established reservations untouched unless downstream validation accepts a change.
