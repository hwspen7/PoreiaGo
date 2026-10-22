# cultural-experience-search: professional travel workflow

## Objective
Identify distinct candidate cultural experiences. This Skill is independently executable through its local `handler.py` entrypoint.

## When to activate
Use when a traveler or itinerary validation specifically needs this capability. Do not invoke on every day simply because the Skill exists. If an answer can be obtained from already validated trip state, reuse that evidence.

## Inputs and prerequisites
Interest, explicit supplied venues or geo radius, names already scheduled. Consult `manifest.json` for the machine-readable field types and `references/input_example.json` for a concrete invocation. Reject or mark partial when required information is missing.

## Execution protocol
1. Extract the relevant traveler constraints, location context, dates and prior confirmed decisions without silently assuming details.
2. Validate provided inputs and retain unambiguous identifiers, geographic coordinates or source metadata where available.
3. Screen for interest relevance, exclude visited places, retain booking unknown status.
4. Compare any alternatives against the actual itinerary state; avoid recommending already scheduled duplicates unless the user deliberately requested them.
5. Return structured output with `status`, `data`, `sources` and `limitations`. Prefer partial or unavailable to fabricated precision.
6. Keep original user state unchanged unless this Skill explicitly authorizes a persistent write.

## Output and downstream use
Return distinct candidate experiences and verification status. Consumers must distinguish confirmed information, mapped candidates, supplied estimates and unknown facts. The planner may select a candidate, but must not turn candidate status into verified status.

## Quality gates
- Do not invent named venues, fares, operating hours, transfer times, accessible routes or reservation availability.
- Never infer source verification from the presence of a URL; compare the exact claim to retrieved evidence when a suitable provider exists.
- Enforce per-skill input bounds and deterministic order to prevent unbounded retries or misleading rankings.
- Preserve the correct location, currency, party size, date and itinerary day rather than copying unrelated context.
- Review duplication against trip registry when relevant and preserve the original itinerary for a repair workflow.

## Failure and recovery
A generic museum is not proof of a bookable hands-on workshop. On missing providers, return `unavailable` or `partial` with an explicit limitation. On ambiguous input, require disambiguation or return a set of alternatives; never guess a country, route or supplier.

## Example input
```json
{
  "interest": "ceramics",
  "exclude_names": [
    "Sample Studio"
  ],
  "candidates": [
    {
      "name": "Other Studio",
      "category": "ceramics"
    }
  ]
}
```

## Scope boundary
This Skill is a bounded specialist. Global day allocation remains with the Blueprint planner, and final acceptance remains with the Quality Gate. External real-time facts must come from providers with preserved provenance. No destination-specific travel facts are hard-coded in this Skill.

## V3.6.3 executable decision specification
1. Filter venue candidates by traveler interest, exclude already booked/visited names, then rank explicit organizer references.
2. An organizer name plus a booking URL is a reference for follow-up, not proof of a bookable workshop.
3. When sessions cannot be checked, label booking_status not_checked and retain candidate source.

### Release acceptance scenario
Run this Skill with normal, ambiguous, malformed and missing-provider inputs. Assert truthful `status`, bounded outputs and preserved provenance. For complete travel tasks, compare the result with the original trip and revalidate any proposed local revision before accepting it.

## Itinerary-first maturity behavior
1. Use a supplied venue catalog where possible, otherwise obtain mapped candidates.
2. Match cultural interest against documented category, description and venue name.
3. Exclude repeated venues from earlier days.
4. Treat organizer and booking links as references, not availability evidence.
5. Distinguish an explicitly identified event/session from an actually reservable workshop.

### Quality and evidence boundary
A crafts museum should not be marketed as a visitor workshop without organizer/session evidence.
