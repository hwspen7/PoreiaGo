"""Independent seasonal-experience-intelligence execution entrypoint."""
from services.advanced_itinerary_skills import seasonal_experience_intelligence


async def run(ctx, args):
    return await seasonal_experience_intelligence(ctx, args)
