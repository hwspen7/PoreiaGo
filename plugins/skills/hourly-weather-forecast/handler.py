"""Produce dated weather risk signals for daily itinerary alternatives."""
from __future__ import annotations

from datetime import date
from services.providers import ProviderUnavailable
from services.skill_support import coordinates, reply, slug


async def run(ctx, args):
    if getattr(ctx, 'providers', None) is None:
        return reply('Weather provider is not configured.', status='unavailable')
    evidence = []
    try:
        position = coordinates(args)
        if position is None:
            city = str(args.get('city') or args.get('destination') or
                       getattr(ctx, 'fields', {}).get('destination') or '').strip()
            if not city:
                return reply('Specify a forecast city or coordinates.', status='partial')
            geo = await ctx.providers.request('geocode', place=city)
            evidence += geo.evidence
            matches = [row for row in geo.data.get('results', []) if isinstance(row, dict)]
            country = slug(args.get('country'))
            if country:
                matches = [row for row in matches if slug(row.get('country')) == country]
            if len({(slug(row.get('country')), str(row.get('latitude')), str(row.get('longitude')))
                    for row in matches}) != 1:
                return reply('Forecast location ambiguous or missing.', status='partial', sources=evidence)
            position = float(matches[0]['latitude']), float(matches[0]['longitude'])
        days = max(1, min(14, int(args.get('forecast_days', 3))))
        forecast = await ctx.providers.request('weather', latitude=position[0],
                                               longitude=position[1], forecast_days=days)
        evidence += forecast.evidence
    except (ValueError, TypeError, KeyError, ProviderUnavailable) as exc:
        return reply('Weather forecast could not be retrieved.', status='unavailable',
                     limitations=[type(exc).__name__], sources=evidence)
    hourly = forecast.data.get('hourly') or {}
    times = hourly.get('time') or []
    temperatures = hourly.get('temperature_2m') or []
    rainfall = hourly.get('precipitation_probability') or []
    requested_date = str(args.get('date') or '').strip()
    if requested_date:
        try:
            date.fromisoformat(requested_date)
        except ValueError:
            return reply('Forecast date must use YYYY-MM-DD.', status='partial', sources=evidence)
    risk_threshold = args.get('rain_threshold_pct', 65)
    if isinstance(risk_threshold, bool) or not isinstance(risk_threshold, (int, float)) or not 0 <= risk_threshold <= 100:
        return reply('Invalid precipitation risk threshold.', status='partial', sources=evidence)
    preview, risky = [], []
    for i, timestamp in enumerate(times[:min(336, days*24)]):
        if requested_date and str(timestamp)[:10] != requested_date:
            continue
        temp = temperatures[i] if i < len(temperatures) else None
        precipitation = rainfall[i] if i < len(rainfall) else None
        item = {'time': timestamp, 'temperature_c': temp, 'rain_probability_pct': precipitation}
        preview.append(item)
        if (isinstance(precipitation, (int, float)) and not isinstance(precipitation, bool)
                and 0 <= precipitation <= 100 and precipitation >= risk_threshold):
            risky.append({'time': timestamp, 'rain_probability_pct': precipitation})
    windows = []
    for item in risky:
        day_and_hour = str(item['time'])
        if windows and str(windows[-1]['end_hour'])[:10] == day_and_hour[:10]:
            # Only coalesce consecutive hourly entries; no invented rainy periods.
            previous = windows[-1]['end_hour']
            try:
                from datetime import datetime, timedelta
                consecutive = datetime.fromisoformat(day_and_hour) - datetime.fromisoformat(previous) == timedelta(hours=1)
            except ValueError:
                consecutive = False
            if consecutive:
                windows[-1]['end_hour'] = day_and_hour
                windows[-1]['peak_probability_pct'] = max(windows[-1]['peak_probability_pct'],
                                                           item['rain_probability_pct'])
                continue
        windows.append({'start_hour': day_and_hour, 'end_hour': day_and_hour,
                        'peak_probability_pct': item['rain_probability_pct']})
    status = 'partial' if requested_date and not preview else 'ok'
    return reply('Dated precipitation risks for outdoor itinerary decisions.', status=status,
                 sources=evidence,
                 data={**forecast.data, 'preview': preview[:12],
                       'rain_risk_windows': risky[:48], 'grouped_rain_windows': windows[:20],
                       'forecast_hours_checked': len(preview), 'requested_date': requested_date or None,
                       'outdoor_plan_review_needed': bool(risky)},
                 limitations=['Rain probability is not a guarantee and cannot establish whether an indoor venue is open.',
                              'A date outside the forecast horizon cannot be independently forecast.'])
