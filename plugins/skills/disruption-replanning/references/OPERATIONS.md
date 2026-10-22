# disruption-replanning — Operational acceptance guide

## Specialist decision rules
1. Treat the input itinerary as immutable while suggesting a repair.
2. Target only affected places or activities, preserving the rest of the planned day.
3. Raise approval requirement for all suggestions and do not cancel an existing booking.
4. After a proposed shift, revalidate time overlap, city base, transfer and future-day reservations.

## Invocation and handoff
The orchestrator selects this specialist only for a matching traveler need. The Skill must receive explicit inputs and return a structured `SkillOutput`. A downstream planner can use candidate records as suggestions, but must retain source status, limitations and all known unknowns.

## Repair policy
When a quality audit flags a defect in this domain, first re-run the Skill using corrected input or additional trustworthy evidence. Submit only the affected activity or metadata field to the repair controller. Reject changes that create a new cross-day duplicate, timeline overlap, unsupported price or venue assertion.

## Minimum acceptance
An accepted result must be schema-compatible, bounded in size, preserve original geography and currency context, and not claim independent fact verification unless evidence was actually checked. Missing external data yields `partial` or `unavailable` rather than confident travel directions.

## V3.6.3 executable decision specification
1. Classify the reported disruption, locate the affected activity indices and shift only the affected suffix if a delay is given.
2. Return a non-mutating proposal with the changed indices and proposed start times; preserve the original input.
3. Require the user to approve a modification and re-run schedule and booking checks after adoption.

### Release acceptance scenario
Run this Skill with normal, ambiguous, malformed and missing-provider inputs. Assert truthful `status`, bounded outputs and preserved provenance. For complete travel tasks, compare the result with the original trip and revalidate any proposed local revision before accepting it.

## Itinerary-first advanced decision rules
1. Work on a copy of the itinerary and preserve untouched activities.
2. Identify affected places by explicit disruption scope and use only supplied alternative names.
3. Exclude previously visited and already scheduled alternatives.
4. Shift only relevant flexible activities; flag confirmed bookings/fixed-time reservations rather than silently moving them.
5. Return proposed replacements and require approval plus final schedule verification before commitment.

### Explicit failure boundary
Do not claim a replacement is open or booked; provide an approval-required proposal.

### Verification checklist
- Inspect output data, ranking reasons, ambiguity and provenance.
- Run normal, malformed, repeated and unavailable-source examples.
- Keep generated itinerary text and established reservations untouched unless downstream validation accepts a change.
