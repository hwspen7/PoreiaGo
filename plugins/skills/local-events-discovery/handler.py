"""Independent local-events-discovery execution entrypoint."""
from aligo.advanced_itinerary_skills import local_events_discovery


async def run(ctx, args):
    return await local_events_discovery(ctx, args)
