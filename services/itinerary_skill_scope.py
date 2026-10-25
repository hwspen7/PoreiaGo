"""The traveler-facing Skills in itinerary-first planning mode.

Providers remain internal infrastructure. Activity planning does not require
flight, airport, accommodation booking, expense ledger or navigation products.
"""
from __future__ import annotations

ACTIVE_ITINERARY_SKILLS = frozenset({
    'place-discovery',
    'place-detail-verification',
    'restaurant-discovery',
    'cultural-experience-search',
    'multi-stop-route-optimizer',
    'schedule-conflict-validation',
    'hourly-weather-forecast',
    'disruption-replanning',
    'trip-budget-allocation',
    'neighborhood-intelligence',
    'opening-hours-intelligence',
    'ticket-entry-planning',
    'local-events-discovery',
    'culinary-specialty-discovery',
    'seasonal-experience-intelligence',
    'accessibility-suitability',
    'day-trip-discovery',
    'itinerary-feasibility-audit',
})

RETIRED_UTILITY_SKILLS = frozenset({
    'airport-transfer',
    'expense-tracking',
    'geocoding',
    'hotel-search',
    'itinerary-export',
    'public-transit-router',
    'source-verification',
    'travel-cost-estimation',
    'walking-directions',
})
