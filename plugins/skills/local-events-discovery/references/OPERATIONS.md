# Operating local-events-discovery

Purpose: Filter date-specific local event candidates using supplied event records.

- Entrypoint: `handler.py:run`
- Input fixture: `references/input_example.json`
- Returned fields: `status`, `data`, `sources`, `limitations`
- Dependency: supplied event records or an explicitly configured `events` provider adapter. No API key is required for supplied records.
- Online data must be supplied through configured providers; these handlers never pretend to query an unavailable API.
- Routing: selected only when user interests, dates, structured evidence or itinerary state justify its use.
- Failure: missing information remains explicitly unverified; never silently infer admission, hours or transport.
- Safety: advisory only, no booking, payment, stored state mutation or hidden network calls.
