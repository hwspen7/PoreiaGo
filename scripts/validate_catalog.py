"""Validate the active itinerary-focused Skill catalog."""
from scripts.verify_v363_skills import verify
expected, actual = verify()
print(f'Validated {actual}/{expected} independently executable itinerary Skills.')
