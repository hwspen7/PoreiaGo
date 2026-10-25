"""Live, read-only provider audit for PoreiaGo's configured ProviderGateway.

Run from the repository root: python -m evaluation.audit_providers
No API keys, full URLs, or credentials are printed or written to disk.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from core.config import settings
from services.providers import ProviderGateway, ProviderUnavailable


def _validate_result(capability: str, payload: dict[str, Any]) -> tuple[str, str]:
    if capability in {"geocode", "places"}:
        results = payload.get("results")
        if not isinstance(results, list):
            return "FAILED", "Response has no valid results list"
        if not results:
            return "EMPTY", "Request succeeded, but no places matched this sample"
        return "PASSED", f"Returned {len(results)} result(s)"
    if capability == "weather":
        hourly = payload.get("hourly")
        if not isinstance(hourly, dict) or not hourly.get("temperature_2m"):
            return "FAILED", "Response does not include hourly temperatures"
        return "PASSED", f"Returned {len(hourly['temperature_2m'])} hourly temperatures"
    if capability == "fx":
        if not isinstance(payload.get("rate"), (float, int)):
            return "FAILED", "Response does not include a numeric exchange rate"
        return "PASSED", f"Reference exchange-rate date: {payload.get('date', 'unknown')}"
    if capability == "walking_route":
        if not isinstance(payload.get("distance_m"), (float, int)):
            return "FAILED", "Response does not include a numeric route distance"
        return "PASSED", "Returned route geometry and distance"
    if capability == "flights":
        if not isinstance(payload.get("offers"), list):
            return "FAILED", "Response does not include an offers list"
        return "PASSED", f"Returned {len(payload['offers'])} offer(s) (may be zero)"
    return "FAILED", "Unknown capability response"


async def audit(args: argparse.Namespace) -> int:
    gateway = ProviderGateway(settings)
    catalog = {item["capability"]: item for item in gateway.catalog()}
    allowed = {"geocode", "weather", "places", "fx", "walking_route", "flights"}
    selected = set(args.only.split(",")) if args.only else allowed
    invalid = selected - allowed
    if invalid:
        print("Invalid capabilities: " + ", ".join(sorted(invalid)))
        return 2

    print("PoreiaGo Provider Integration Audit")
    print(f"Online enabled: {settings.allow_online}")
    print("This checks real providers. It does not test LLM routing or the user interface.")
    print("No API keys will be displayed. External calls may consume provider quotas.\n")
    examples = {
        "geocode": dict(place=args.city),
        "weather": dict(latitude=args.lat, longitude=args.lon, forecast_days=1),
        "places": dict(latitude=args.lat, longitude=args.lon, kind="tourism", radius=2500),
        "fx": dict(amount=10, origin="USD", destination="EUR"),
        "walking_route": dict(waypoints=[
            {"latitude": args.lat, "longitude": args.lon},
            {"latitude": args.lat + 0.002, "longitude": args.lon + 0.002},
        ]),
        "flights": dict(origin="LHR", destination="CDG",
                        departure_date=(date.today() + timedelta(days=21)).isoformat(),
                        passengers=1),
    }
    outcomes = []
    for name in ["geocode", "weather", "places", "fx", "walking_route", "flights"]:
        if name not in selected:
            continue
        item = catalog.get(name, {})
        provider_name = item.get("provider", "not registered")
        if name == "flights" and not args.with_flights:
            status, detail = "SKIPPED", "Flight API calls require explicit --with-flights opt-in"
        elif name == "walking_route" and not args.with_routing:
            status, detail = "SKIPPED", "Routing API calls require explicit --with-routing opt-in"
        elif not item.get("configured", False):
            status, detail = "SKIPPED", "Provider is not configured or online access is disabled"
        else:
            t0 = time.monotonic()
            try:
                result = await gateway.request(name, **examples[name])
                status, detail = _validate_result(name, result.data)
                provider_name = (
                    result.evidence[0].provider
                    if result.evidence else provider_name
                )
            except ProviderUnavailable as exc:
                status, detail = "FAILED", str(exc)
            except Exception as exc:
                # Never print potentially credential-containing request URLs.
                status, detail = "FAILED", f"Unexpected {type(exc).__name__}"
            elapsed = round(time.monotonic() - t0, 2)
            outcomes.append({"capability": name, "provider": provider_name,
                             "status": status, "detail": detail, "seconds": elapsed})
            print(f"[{status:7s}] {name:14s} {provider_name}: {detail} ({elapsed:.2f}s)")
            continue
        outcomes.append({"capability": name, "provider": provider_name,
                         "status": status, "detail": detail, "seconds": None})
        print(f"[{status:7s}] {name:14s} {provider_name}: {detail}")

    failures = sum(item["status"] == "FAILED" for item in outcomes)
    successes = sum(item["status"] == "PASSED" for item in outcomes)
    print(f"\nSummary: {successes} passed, {failures} failed, "
          f"{sum(item['status'] == 'SKIPPED' for item in outcomes)} skipped, "
          f"{sum(item['status'] == 'EMPTY' for item in outcomes)} empty.")
    if args.output:
        path = Path(args.output)
        if path.exists() and not args.overwrite:
            print(f"Report exists, not overwritten: {path}")
        else:
            path.write_text(json.dumps({"online_enabled": settings.allow_online,
                                        "results": outcomes}, indent=2), encoding="utf-8")
            print(f"Saved redacted report: {path}")
    return 1 if failures else 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--city", default="Paris", help="Sample city for geocoding")
    parser.add_argument("--lat", type=float, default=48.8566,
                        help="Latitude for weather, POI, and optional route checks")
    parser.add_argument("--lon", type=float, default=2.3522,
                        help="Longitude for weather, POI, and optional route checks")
    parser.add_argument("--only", default="", help="Comma-separated capabilities to audit")
    parser.add_argument("--with-routing", action="store_true",
                        help="Opt in to a routing request (can use provider quota)")
    parser.add_argument("--with-flights", action="store_true",
                        help="Opt in to a flight offer request (can use provider quota)")
    parser.add_argument("--output", default="",
                        help="Output JSON file; use empty string to skip saving")
    parser.add_argument("--overwrite", action="store_true",
                        help="Allow replacing existing report")
    args = parser.parse_args()
    try:
        sys.exit(asyncio.run(audit(args)))
    except KeyboardInterrupt:
        print("Audit interrupted")
        sys.exit(130)


if __name__ == "__main__":
    main()
