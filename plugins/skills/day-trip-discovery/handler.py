"""Independent day-trip-discovery execution entrypoint."""
from aligo.advanced_itinerary_skills import day_trip_discovery


async def run(ctx, args):
    return await day_trip_discovery(ctx, args)
