"""Independent culinary-specialty-discovery execution entrypoint."""
from services.advanced_itinerary_skills import culinary_specialty_discovery


async def run(ctx, args):
    return await culinary_specialty_discovery(ctx, args)
