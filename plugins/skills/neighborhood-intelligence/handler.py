"""Independent neighborhood-intelligence execution entrypoint."""
from aligo.advanced_itinerary_skills import neighborhood_intelligence


async def run(ctx, args):
    return await neighborhood_intelligence(ctx, args)
