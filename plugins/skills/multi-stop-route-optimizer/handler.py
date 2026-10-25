"""Optimize daily visit order with fixed endpoints and explicit geographic limitations.

Haversine is used for proximity only; it cannot assert walking/rail time.
"""
from __future__ import annotations

from math import radians, cos, sin, asin, sqrt, isfinite
from services.skill_support import reply, coordinates, slug


def km(a, b):
    la, lo = coordinates(a)
    lb, lob = coordinates(b)
    dlat, dlon = radians(lb - la), radians(lob - lo)
    h = sin(dlat / 2)**2 + cos(radians(la))*cos(radians(lb))*sin(dlon / 2)**2
    return 12742 * asin(min(1.0, sqrt(h)))


def _length(order, points, cycle):
    legs = [km(points[i], points[j]) for i, j in zip(order, order[1:])]
    if cycle:
        legs.append(km(points[order[-1]], points[order[0]]))
    return sum(legs), legs


async def run(ctx, args):
    points = args.get('waypoints') or []
    if not isinstance(points, list) or not 2 <= len(points) <= 20:
        return reply('Provide between 2 and 20 waypoints.', status='partial')
    try:
        for row in points:
            if not isinstance(row, dict) or coordinates(row) is None:
                raise ValueError('Every waypoint must contain valid coordinates.')
        fixed_last = args.get('fixed_destination', False)
        cycle = args.get('return_to_start', False)
        if not isinstance(fixed_last, bool) or not isinstance(cycle, bool) or (fixed_last and cycle):
            raise ValueError('Fixed destination and return-to-start are mutually exclusive.')
        initial = list(range(len(points)))
        initial_cost, _ = _length(initial, points, cycle)
        remaining = list(range(1, len(points) - int(fixed_last)))
        route = [0]
        while remaining:
            next_stop = min(remaining, key=lambda i: (km(points[route[-1]], points[i]), i))
            route.append(next_stop)
            remaining.remove(next_stop)
        if fixed_last:
            route.append(len(points)-1)
        # The first stop and any fixed destination are immutable.
        for _ in range(3):
            changed = False
            last_exclusive = len(route) - int(fixed_last)
            for i in range(1, last_exclusive-1):
                for j in range(i+1, last_exclusive):
                    candidate = route[:i] + route[i:j+1][::-1] + route[j+1:]
                    if _length(candidate, points, cycle)[0] < _length(route, points, cycle)[0] - 1e-8:
                        route = candidate
                        changed = True
            if not changed:
                break
        total, legs = _length(route, points, cycle)
        max_leg = args.get('max_leg_km')
        if max_leg is not None:
            max_leg = float(max_leg)
            if not isfinite(max_leg) or max_leg <= 0:
                raise ValueError('Invalid leg-distance threshold.')
        max_total = args.get('max_total_km')
        if max_total is not None:
            max_total = float(max_total)
            if not isfinite(max_total) or max_total <= 0:
                raise ValueError('Invalid route-distance threshold.')
        overlong = [i for i, distance in enumerate(legs) if max_leg is not None and distance > max_leg]
        area_switches = []
        for i in range(1, len(route)):
            prev = slug(points[route[i-1]].get('area'))
            area = slug(points[route[i]].get('area'))
            if prev and area and prev != area:
                area_switches.append(i)
    except (KeyError, ValueError, TypeError) as exc:
        return reply('Invalid daily route.', status='partial', limitations=[str(exc)])
    warnings = []
    if overlong:
        warnings.append('Some straight-line legs exceed the requested proximity threshold.')
    if max_total is not None and total > max_total:
        warnings.append('Total proximity distance exceeds the requested limit.')
    if area_switches:
        warnings.append('Route crosses explicitly named areas; confirm actual transit connections.')
    return reply('Daily stop ordering optimized as a geographic proximity proxy.', status='partial',
                 data={'ordered_stops': [points[i] for i in route], 'ordered_indices': route,
                       'leg_distances_km_straight_line': [round(x, 3) for x in legs],
                       'total_km_straight_line': round(total, 3),
                       'original_km_straight_line': round(initial_cost, 3),
                       'improvement_km': round(max(0, initial_cost-total), 3),
                       'method': 'nearest_neighbor_haversine_2opt',
                       'fixed_origin': True, 'fixed_destination': fixed_last,
                       'overlong_leg_indices': overlong, 'area_switch_indices': area_switches,
                       'distance_limit_exceeded': max_total is not None and total > max_total,
                       'planning_warnings': warnings},
                 limitations=['Straight-line distance cannot validate real walking, rail travel, accessibility or venue hours.'] + warnings)
