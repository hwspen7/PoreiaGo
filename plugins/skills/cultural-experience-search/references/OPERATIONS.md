# cultural-experience-search — Operational acceptance guide

## Specialist decision rules
1. Distinguish public museums and craft shops from bookable participatory workshops.
2. Eliminate exact repeat venues already scheduled elsewhere in the itinerary.
3. Require booking date, language, minimum age and operating organizer before calling a workshop confirmed.
4. Prefer distinct experience types across days when travelers ask for variety.

## Invocation and handoff
The orchestrator selects this specialist only for a matching traveler need. The Skill must receive explicit inputs and return a structured `SkillOutput`. A downstream planner can use candidate records as suggestions, but must retain source status, limitations and all known unknowns.

## Repair policy
When a quality audit flags a defect in this domain, first re-run the Skill using corrected input or additional trustworthy evidence. Submit only the affected activity or metadata field to the repair controller. Reject changes that create a new cross-day duplicate, timeline overlap, unsupported price or venue assertion.

## Minimum acceptance
An accepted result must be schema-compatible, bounded in size, preserve original geography and currency context, and not claim independent fact verification unless evidence was actually checked. Missing external data yields `partial` or `unavailable` rather than confident travel directions.

## V3.6.3 executable decision specification
1. Filter venue candidates by traveler interest, exclude already booked/visited names, then rank explicit organizer references.
2. An organizer name plus a booking URL is a reference for follow-up, not proof of a bookable workshop.
3. When sessions cannot be checked, label booking_status not_checked and retain candidate source.

### Release acceptance scenario
Run this Skill with normal, ambiguous, malformed and missing-provider inputs. Assert truthful `status`, bounded outputs and preserved provenance. For complete travel tasks, compare the result with the original trip and revalidate any proposed local revision before accepting it.

## Itinerary-first advanced decision rules
1. Use a supplied venue catalog where possible, otherwise obtain mapped candidates.
2. Match cultural interest against documented category, description and venue name.
3. Exclude repeated venues from earlier days.
4. Treat organizer and booking links as references, not availability evidence.
5. Distinguish an explicitly identified event/session from an actually reservable workshop.

### Explicit failure boundary
A crafts museum should not be marketed as a visitor workshop without organizer/session evidence.

### Verification checklist
- Inspect output data, ranking reasons, ambiguity and provenance.
- Run normal, malformed, repeated and unavailable-source examples.
- Keep generated itinerary text and established reservations untouched unless downstream validation accepts a change.
