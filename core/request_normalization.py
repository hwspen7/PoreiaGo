"""Conservative fallback normalization for user-authored trip constraints.

Ambiguous information remains unresolved for the model/provider rather than
silently inventing units. Callers may override fields from explicit trusted
request context. All produced clock times are destination-local, not UTC.
"""
from __future__ import annotations
import re

_WORD_NUMBERS = {
    'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5,
    'six': 6, 'seven': 7, 'eight': 8, 'nine': 9, 'ten': 10,
    'eleven': 11, 'twelve': 12, 'thirteen': 13, 'fourteen': 14,
    'fifteen': 15, 'sixteen': 16, 'seventeen': 17, 'eighteen': 18,
    'nineteen': 19, 'twenty': 20,
}


def trip_window(text: str) -> dict:
    """Extract only explicitly requested duration/start; no presumed schedule."""
    result = {}
    words = '|'.join(_WORD_NUMBERS)
    match = re.search(r'\b(\d{1,2}|' + words +
                      r')\s*(?:-\s*)?(?:hours?|hrs?)\b', text, re.I)
    if match:
        raw = match.group(1).lower()
        hours = int(raw) if raw.isdecimal() else _WORD_NUMBERS[raw]
        if 1 <= hours <= 23:
            result['duration_minutes'] = hours * 60
            result['days'] = 1
    # Explicit start language avoids mistaking an appointment mentioned in
    # passing for the departure time of the entire itinerary.
    clock = re.search(
        r'\b(?:start|starts|starting|begin|begins|beginning|depart|departing)'
        r'\s+(?:at\s+)?(\d{1,2})(?::([0-5]\d))?\s*(am|pm)?\b',
        text, re.I)
    if clock:
        hour, minute = int(clock.group(1)), int(clock.group(2) or 0)
        ampm = (clock.group(3) or '').lower()
        if ampm and 1 <= hour <= 12:
            hour = hour % 12 + (12 if ampm == 'pm' else 0)
        if 0 <= hour <= 23:
            result['start_time'] = f'{hour:02d}:{minute:02d}'
            if 'duration_minutes' in result:
                end = hour * 60 + minute + result['duration_minutes']
                if end <= 1440:
                    result['end_time'] = f'{end // 60:02d}:{end % 60:02d}'
                # Crossing a midnight boundary is not a same-day excursion.
                else:
                    result.pop('days', None)
    return result


def budget_currency(matched: str, first_code: str | None,
                    last_code: str | None) -> dict:
    """Resolve explicit monetary units without conflating CNY and JPY yen."""
    explicit = [code.upper() for code in (first_code, last_code) if code]
    if len(set(explicit)) > 1:
        return {'currency_ambiguous': True}
    if explicit:
        return {'currency': explicit[0]}
    if '€' in matched:
        return {'currency': 'EUR'}
    if '£' in matched:
        return {'currency': 'GBP'}
    if '¥' in matched or '￥' in matched:
        return {'currency_ambiguous': True, 'currency_candidates': ['CNY', 'JPY']}
    if '$' in matched:
        # Keep the historical USD convention for $, whose locale still needs
        # confirmation when a non-US destination is involved.
        return {'currency': 'USD'}
    return {'currency_ambiguous': True}
