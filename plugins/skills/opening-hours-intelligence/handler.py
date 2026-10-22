"""Independent opening-hours-intelligence execution entrypoint."""
from aligo.advanced_itinerary_skills import opening_hours_intelligence


async def run(ctx, args):
    return await opening_hours_intelligence(ctx, args)
