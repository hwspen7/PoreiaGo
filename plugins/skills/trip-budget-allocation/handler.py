"""Allocate an itinerary budget with explicit party basis and daily envelopes."""
from __future__ import annotations

import math
import re

from services.skill_support import finite_price, reply


async def run(ctx, args):
    fields = getattr(ctx, 'fields', {}) or {}
    amount = finite_price(args.get('total', args.get('budget', fields.get('budget'))))
    if amount is None or amount <= 0:
        return reply('Supply a positive trip budget.', status='partial')
    try:
        days = int(args.get('days', fields.get('days', 1) or 1))
        travelers = int(args.get('travelers', fields.get('travelers', 1) or 1))
    except (ValueError, TypeError):
        return reply('Days and travelers must be positive integers.', status='partial')
    if not 1 <= days <= 30 or not 1 <= travelers <= 30:
        return reply('Days or traveler count outside supported limits.', status='partial')
    if fields.get('currency_ambiguous') and not fields.get('currency'):
        return reply('Budget currency is ambiguous. Confirm an ISO currency code before allocation.',
                     status='partial',
                     data={'currency_candidates': fields.get('currency_candidates', [])})
    currency = str(args.get('currency') or fields.get('currency') or 'USD').upper()
    if not re.fullmatch(r'[A-Z]{3}', currency):
        return reply('Use an ISO-style three-letter currency code.', status='partial')
    basis = str(args.get('basis') or fields.get('budget_basis') or 'group_total')
    if basis not in {'per_person', 'group_total'}:
        return reply('Budget basis must be per_person or group_total.', status='partial')
    weights = args.get('weights') or {'accommodation': 36, 'transport': 25,
                                     'food': 20, 'activities': 12, 'contingency': 7}
    if (not isinstance(weights, dict) or not 1 <= len(weights) <= 15
            or any(not isinstance(k, str) or not k.strip() or
                   not isinstance(v, (int, float)) or isinstance(v, bool) or
                   not math.isfinite(v) or v < 0 for k, v in weights.items())):
        return reply('Invalid budget categories or percentage weights.', status='partial')
    if abs(sum(weights.values())-100) > 0.001:
        return reply('Budget weight percentages must sum to 100.', status='partial')
    allocation = {category: round(amount*weight/100, 2) for category, weight in weights.items()}
    first = next(iter(allocation))
    allocation[first] = round(allocation[first] + amount-sum(allocation.values()), 2)
    group_total = round(amount*travelers, 2) if basis == 'per_person' else amount
    per_person = amount if basis == 'per_person' else round(amount/travelers, 2)
    daily_weights = args.get('day_weights')
    if daily_weights is None:
        daily_weights = [1.0]*days
    if (not isinstance(daily_weights, list) or len(daily_weights) != days or any(
            not isinstance(x, (int, float)) or isinstance(x, bool) or
            not math.isfinite(x) or x < 0 for x in daily_weights) or sum(daily_weights) <= 0):
        return reply('Day weights must be a nonnegative list for every trip day.', status='partial')
    # Daily spending envelope excludes accommodation and transport by default.
    # This is a planning choice, not a claim of destination costs.
    on_site_categories = args.get('day_categories') or ['food', 'activities']
    if (not isinstance(on_site_categories, list) or any(not isinstance(x, str) for x in on_site_categories)
            or len(on_site_categories) > len(weights)):
        return reply('Invalid daily envelope categories.', status='partial')
    on_site_total = sum(allocation.get(name, 0.0) for name in on_site_categories)
    daily = [round(on_site_total*weight/sum(daily_weights), 2) for weight in daily_weights]
    daily[-1] = round(daily[-1] + on_site_total-sum(daily), 2)
    return reply('Itinerary budget envelopes computed; no prices were invented.',
                 data={'total': amount, 'currency': currency, 'days': days,
                       'travelers': travelers, 'daily_average': round(amount/days, 2),
                       'allocation': allocation, 'per_person_total': per_person,
                       'group_total': group_total, 'input_basis': basis,
                       'contingency_amount': allocation.get('contingency', 0),
                       'daily_on_site_envelopes': daily,
                       'daily_envelope_basis': basis,
                       'daily_envelope_categories': on_site_categories,
                       'verification_level': 'allocation_only'},
                 limitations=['This allocates money; it does not estimate actual venue prices, hotel rates or currency exchange.'])
