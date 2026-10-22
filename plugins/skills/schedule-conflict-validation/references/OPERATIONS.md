# schedule-conflict-validation — Operational acceptance guide

## Specialist decision rules
1. Duration intervals must have start earlier than end and a consistent timezone interpretation.
2. Check direct overlaps and caller-provided transition buffers between consecutive activities.
3. Malformed activities must be reported, not quietly considered non-conflicting.
4. The repair coordinator must re-run checks after changing an activity time.

## Invocation and handoff
The orchestrator selects this specialist only for a matching traveler need. The Skill must receive explicit inputs and return a structured `SkillOutput`. A downstream planner can use candidate records as suggestions, but must retain source status, limitations and all known unknowns.

## Repair policy
When a quality audit flags a defect in this domain, first re-run the Skill using corrected input or additional trustworthy evidence. Submit only the affected activity or metadata field to the repair controller. Reject changes that create a new cross-day duplicate, timeline overlap, unsupported price or venue assertion.

## Minimum acceptance
An accepted result must be schema-compatible, bounded in size, preserve original geography and currency context, and not claim independent fact verification unless evidence was actually checked. Missing external data yields `partial` or `unavailable` rather than confident travel directions.

## V3.6.3 executable decision specification
1. Parse all event ISO datetime ranges; reject end-before-start, invalid buffers and mixed timezone interpretation.
2. Compare every interval pair for intersections so nested long/short activities are all detected.
3. Apply explicit transfer buffers to successive activities; geographic travel time needs routing evidence.

### Release acceptance scenario
Run this Skill with normal, ambiguous, malformed and missing-provider inputs. Assert truthful `status`, bounded outputs and preserved provenance. For complete travel tasks, compare the result with the original trip and revalidate any proposed local revision before accepting it.

## Itinerary-first advanced decision rules
1. Parse all ISO timestamps and reject negative or missing-duration events.
2. Check every overlapping interval pair, including a long visit covering two short ones.
3. For consecutive events, require the maximum of the previous outgoing transfer buffer and the next incoming transfer+safety buffer.
4. Report overlap and insufficient transfer buffer separately, with exact index and shortfall.
5. Reject mixed aware/naive time bases and surface invalid events rather than silently accepting them.

### Explicit failure boundary
A timetable conflict must never be hidden because an optional specialist is unavailable; core planning guards remain authoritative.

### Verification checklist
- Inspect output data, ranking reasons, ambiguity and provenance.
- Run normal, malformed, repeated and unavailable-source examples.
- Keep generated itinerary text and established reservations untouched unless downstream validation accepts a change.
