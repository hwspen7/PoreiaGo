"""Independent itinerary-feasibility-audit execution entrypoint."""
from aligo.advanced_itinerary_skills import itinerary_feasibility_audit


async def run(ctx, args):
    return await itinerary_feasibility_audit(ctx, args)
