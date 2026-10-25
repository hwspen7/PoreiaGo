"""Independent ticket-entry-planning execution entrypoint."""
from services.advanced_itinerary_skills import ticket_entry_planning


async def run(ctx, args):
    return await ticket_entry_planning(ctx, args)
