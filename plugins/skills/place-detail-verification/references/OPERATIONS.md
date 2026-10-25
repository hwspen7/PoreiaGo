# place-detail-verification — Operational acceptance guide

## Specialist decision rules
1. Compare canonical name and explicitly supplied aliases, not invented translations.
2. Require a city match when venue names occur in many places and structured address is available.
3. Do not infer that an unseen establishment does not exist; the candidate search could be incomplete.
4. Keep place existence evidence separate from activity hours, tickets, accessibility and seasonal access.

## Invocation and handoff
The orchestrator selects this specialist only for a matching traveler need. The Skill must receive explicit inputs and return a structured `SkillOutput`. A downstream planner can use candidate records as suggestions, but must retain source status, limitations and all known unknowns.

## Repair policy
When a quality audit flags a defect in this domain, first re-run the Skill using corrected input or additional trustworthy evidence. Submit only the affected activity or metadata field to the repair controller. Reject changes that create a new cross-day duplicate, timeline overlap, unsupported price or venue assertion.

## Minimum acceptance
An accepted result must be schema-compatible, bounded in size, preserve original geography and currency context, and not claim independent fact verification unless evidence was actually checked. Missing external data yields `partial` or `unavailable` rather than confident travel directions.

## V3.6.3 executable decision specification
1. Match explicit place identifiers or exact names/aliases, then narrow by known city or country.
2. Multiple distinct candidates are ambiguous and cannot be promoted to a single confirmed identity.
3. Even an API place-detail response cannot itself verify current hours, admission, bookable sessions or accessibility.

### Release acceptance scenario
Run this Skill with normal, ambiguous, malformed and missing-provider inputs. Assert truthful `status`, bounded outputs and preserved provenance. For complete travel tasks, compare the result with the original trip and revalidate any proposed local revision before accepting it.

## Itinerary-first advanced decision rules
1. Use exact aliases or a stable place_id, never substring-based place identity.
2. If place_id is supplied, reject candidates with another ID even when the name matches.
3. Apply city and country restrictions against explicit fields first, then address if fields are absent.
4. Match provider detail results against the requested place_id and geographic scope.
5. Return an ambiguous/unknown status rather than turning candidate matching into factual verification.

### Explicit failure boundary
A missing candidate is unknown, not proof a place has closed or does not exist.

### Verification checklist
- Inspect output data, ranking reasons, ambiguity and provenance.
- Run normal, malformed, repeated and unavailable-source examples.
- Keep generated itinerary text and established reservations untouched unless downstream validation accepts a change.
