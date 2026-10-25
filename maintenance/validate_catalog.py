"""Validate the active itinerary-focused Skill Catalog."""
from maintenance.verify_legacy_skills import verify
expected, actual = verify()
print(f'Validated {actual}/{expected} independently executable itinerary Skills.')
