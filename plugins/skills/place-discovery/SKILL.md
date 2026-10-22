# place-discovery

## Purpose
Place Discovery ranks supplied, explicitly identified venue candidates for itinerary planning. When configured, it may request external mapped places using the supported provider interface. Candidate discovery alone does not establish current opening, accessibility, pricing, reservations, or booking availability.

## Implementation
- Entry point: `handler.py:run` (async)
- Manifest: `manifest.json`
- Test fixture: `references/input_example.json`
- Source and execution limits: `references/OPERATIONS.md`
- State mutation: none

## Itinerary-first maturity behavior
The output is grounded in candidate records. Unknown fields are not silently promoted to verified facts. Missing provider access yields a qualified partial or unavailable result.

## Execution protocol
1. Confirm the requested destination or supplied coordinates before discovery.
2. Prefer caller-supplied candidates when valid records are present.
3. Reject malformed candidate collections rather than guessing records.
4. Use the provider only when it is available and explicitly enabled.
5. Never infer opening times from a venue name.
6. Never infer prices, currency, or reservation availability.
7. Never infer disability access from an OSM mapping pin.
8. Preserve stable place IDs when the provider supplies them.
9. Keep OSM identifiers and place identifiers distinct.
10. Use original geographic coordinates without reverse-geocoding guesses.
11. Keep original available addresses and neighborhood labels.
12. Bound the candidate set before ranking.
13. Suppress repeated identities in the output.
14. Respect caller exclusions before applying relevance ranking.
15. Use cuisine or category tags only when supplied.
16. Keep unknown tag matches as uncertain rather than verified.
17. Return an explicit status for unconfigured providers.
18. Retain source references when a provider returns evidence.
19. Keep supplied candidate data distinct from provider-backed candidates.
20. Carry through opening-hour metadata as unverified input.
21. Carry through admission metadata as unverified input.
22. Carry through accessibility metadata as unverified input.
23. Preserve the requested output limit and bounded ordering.
24. Never fabricate travel times or route geometry.
25. Do not automatically book, purchase, or mutate a saved itinerary.
26. Provide concise limitations for unknown critical facts.
27. Return predictable schema-based fields in all supported output states.
28. Propagate only safe error classifications to the user.
29. Use the operations reference for expected input shapes.
30. Run documented example fixtures without a live network connection.
31. Run specialist unit tests before releasing a manifest update.
32. Run registry discovery tests after changing metadata.
33. Run the eighteen-Skill verification script before packaging.
34. Run frontend and backend contract tests after integration changes.
35. Confirm provider observations have not been presented as certified facts.
36. Verify unavailable is distinct from absent or nonexistent.
37. Verify candidate ranking never implies current service availability.
38. Audit for secrets before exporting or committing the source tree.
39. Maintain compatibility with the public Skill Registry contract.
40. Inspect human-facing result wording for provenance transparency.
