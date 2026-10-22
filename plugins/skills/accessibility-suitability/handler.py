"""Independent accessibility-suitability execution entrypoint."""
from aligo.advanced_itinerary_skills import accessibility_suitability


async def run(ctx, args):
    return await accessibility_suitability(ctx, args)
