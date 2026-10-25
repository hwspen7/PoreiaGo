# Operating itinerary-feasibility-audit

Purpose: Non-destructively flag itinerary overlaps, repeated place identifiers, and unverified activities.

- Entrypoint: `handler.py:run`
- Input fixture: `references/input_example.json`
- Returned fields: `status`, `data`, `sources`, `limitations`
- Dependency: supplied candidate records. No API key or extra package is required.
- Online data must be supplied through configured providers; these handlers never pretend to query an unavailable API.
- Routing: selected only when user interests, dates, structured evidence or itinerary state justify its use.
- Failure: missing information remains explicitly unverified; never silently infer admission, hours or transport.
- Safety: advisory only, no booking, payment, stored state mutation or hidden network calls.
