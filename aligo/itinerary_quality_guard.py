"""Conservative, itinerary-first quality controls for generated travel days.

Source candidates are useful for resolving a name or locality, but missing a
name from a bounded search is never proof that the place is fictional.
"""
from __future__ import annotations

from copy import deepcopy
import re
from typing import Any

from .pipeline_integrity import named_weather_venues


def _key(value: Any) -> str:
    return " ".join(re.findall(r"[^\W_]+", str(value or "").casefold()))


def _weather_key(name: str) -> str:
    label = _key(name)
    return re.sub(r"^(?:(?:visit|explore|tour|see|inside|the|nearby)\s+)+", "", label)


def _weather_keys(text: str) -> set[str]:
    return {_weather_key(name) for name in named_weather_venues(text)}


def _candidate_for_activity(activity: dict, candidates: list[dict]) -> dict | None:
    name = _key(activity.get("place_name"))
    description = _key(activity.get("description"))
    if not name and not description:
        return None
    for item in candidates:
        label = _key(item.get("name"))
        if not label:
            continue
        if name and name == label:
            return item
        if not name and len(label) >= 9 and re.search(
                r"(?<!\w)" + re.escape(label) + r"(?!\w)", description):
            return item
    return None


def day_evidence_issues(day: dict, skill_context: dict) -> list[str]:
    """Only reject locality claims contradicted by a structured place record.

    Addresses and coordinates do not establish a neighborhood in the absence of
    a geospatial comparison; only an explicit provider area can be compared.
    """
    candidates = [c for c in skill_context.get("places", []) +
                  skill_context.get("experiences", []) if isinstance(c, dict)]
    issues = []
    for index, activity in enumerate(day.get("activities") or [], 1):
        if not isinstance(activity, dict):
            continue
        match = _candidate_for_activity(activity, candidates)
        if match is None:
            continue
        actual = _key(match.get("area") or match.get("neighborhood"))
        claimed = _key(activity.get("area"))
        if actual and claimed and actual != claimed:
            # A locality may include a larger district; reject only when both
            # contain independently stated, mutually exclusive area names.
            if actual not in claimed and claimed not in actual:
                issues.append(
                    f"Day {day.get('day')} activity {index}: {match.get('name')} is "
                    f"listed in {match.get('area') or match.get('neighborhood')}, "
                    f"not the asserted area {activity.get('area')}. "
                    "Correct the location or choose a different stop.")
    return issues


def reconcile_candidate_locality(day: dict, skill_context: dict) -> tuple[dict, list[str]]:
    """Withdraw a contradicted area claim without replacing it with another guess.

    A map candidate's area is not independently certified. The safe recovery is
    to remove the unsupported activity area and request location verification;
    do not rewrite the route or claim that the source is authoritative.
    """
    revised = deepcopy(day)
    notes = []
    candidates = [row for row in (skill_context.get('places') or []) +
                  (skill_context.get('experiences') or []) if isinstance(row, dict)]
    for activity in revised.get('activities') or []:
        if not isinstance(activity, dict) or not activity.get('area'):
            continue
        row = _candidate_for_activity(activity, candidates)
        if not row:
            continue
        asserted = _key(activity['area'])
        mapped = _key(row.get('area') or row.get('neighborhood'))
        if not mapped or asserted == mapped or mapped in asserted or asserted in mapped:
            continue
        activity.pop('area', None)
        current = str(activity.get('verification_needed') or '').strip()
        reminder = 'Confirm the exact venue location and neighborhood before visiting.'
        activity['verification_needed'] = (
            (current.rstrip(' .') + '. ' if current else '') + reminder)[:260]
        notes.append('Removed an activity neighborhood contradicted by source candidate metadata.')
    return revised, notes


def _indoor_location(day: dict, forbidden: set[str]) -> str:
    indoor = re.compile(r"\b(museum|conservatory|gallery|aquarium|library|"
                        r"indoor market|science center|cultural center|statehouse|visitor center)\b", re.I)
    for item in day.get("activities") or []:
        if not isinstance(item, dict):
            continue
        description = str(item.get("description") or "")
        if not indoor.search(description):
            continue
        name = str(item.get("place_name") or "").strip()
        if name and _key(name) not in forbidden:
            return name
        named = re.search(r"\b(?:[A-Z][A-Za-z'-]+\s+){1,5}"
                          r"(?:Museum|Conservatory|Gallery|Aquarium|Library|Statehouse|Visitor Center)\b",
                          description)
        if named and _key(named.group()) not in forbidden:
            return named.group()
    return ""


def reconcile_daily_content(day: dict, previous_days: list[dict], *,
                            request: str, final_day: bool = False) -> tuple[dict, list[str]]:
    """Fix only wording omissions using existing day facts, never invent venues.

    Changes are bounded, read-only with respect to the input, and leave timing,
    named activities, routing and verified bookings untouched.
    """
    output = deepcopy(day)
    changes = []
    previous = set()
    for older in previous_days:
        previous.update(_weather_keys(
            str(older.get("weather_alternative") or "")))
    backup = str(output.get("weather_alternative") or "")
    present = _weather_keys(backup)
    repeated = {name for name in present if name in previous}
    if repeated:
        fresh = _indoor_location(output, previous)
        if fresh:
            output["weather_alternative"] = (
                f"If rain affects outdoor stops, prioritize the existing "
                f"visit to {fresh} where indoor access is available; check "
                "opening hours and ticket conditions before setting out.")
            changes.append("Replaced a repeated rain venue using an indoor stop from this day.")
        else:
            # Do not fabricate a replacement attraction; leave a transparent
            # warning for a subsequent model or traveler review.
            changes.append("Repeated rain venue needs a separately verified local alternative.")
    food_request = bool(re.search(
        r"\b(food|culinary|dining|cuisine|restaurant|meals?)\b", request, re.I))
    number = int(output.get("day") or 0)
    if food_request and number > 1 and not final_day:
        has_dinner = any(
            item.get("meal_type") == "dinner" or
            re.search(r"\b(dinner|supper|evening meal)\b",
                      str(item.get("description") or ""), re.I)
            for item in output.get("activities") or [] if isinstance(item, dict))
        notes = str(output.get("notes") or "")
        if not has_dinner and not re.search(r"\b(dinner|supper|evening meal)\b", notes, re.I):
            area = str(output.get("area_cluster") or "local neighborhood").strip()
            addition = (f"Optional dinner: choose a restaurant in {area} "
                        "or near the overnight base; verify current hours and menu.")
            if len(notes) + len(addition) + 2 <= 900:
                output["notes"] = (notes.rstrip(" .") + ". " if notes else "") + addition
                changes.append("Added an optional local dinner without inventing a restaurant.")
    return output, changes
