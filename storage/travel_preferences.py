"""Validated, consent-gated long-term travel profile stored in the existing preferences table.

The profile is deliberately separate from trip-specific, protected itinerary constraints.
It supplies planning defaults and never constitutes verified provider evidence.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator

PROFILE_CATEGORY = 'travel_profile_v1'

Pace = Literal['no_preference', 'relaxed', 'balanced', 'active']
Budget = Literal['no_preference', 'budget', 'mid_range', 'premium', 'luxury']
Crowd = Literal['quiet', 'local_authentic', 'lively_social', 'avoid_crowds']
Interest = Literal['history_culture', 'nature', 'museums_art', 'architecture', 'aviation',
                   'shopping', 'outdoor_adventures', 'entertainment']
Transport = Literal['walking', 'public_transit', 'rental_car', 'taxi_rideshare', 'cycling']
Accommodation = Literal['hotels', 'boutique_hotels', 'apartments', 'resorts', 'hostels']
Food = Literal['local_cuisine', 'street_food', 'casual_dining', 'fine_dining',
               'vegetarian', 'vegan', 'halal', 'kosher']
Access = Literal['avoid_long_walks', 'step_free_access', 'wheelchair_accessible',
                 'frequent_rest_breaks']

class TravelProfile(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)

    travel_pace_schedule: Pace = 'no_preference'
    interests_experiences: list[Interest] = Field(default_factory=list, max_length=8)
    budget_spending: Budget = 'no_preference'
    transportation: list[Transport] = Field(default_factory=list, max_length=5)
    accommodation: list[Accommodation] = Field(default_factory=list, max_length=5)
    food_dining: list[Food] = Field(default_factory=list, max_length=8)
    accessibility_essential_needs: list[Access] = Field(default_factory=list, max_length=4)
    crowds_atmosphere: list[Crowd] = Field(default_factory=list, max_length=4)

    @model_validator(mode='after')
    def unique_selections(self):
        for key in ('interests_experiences', 'transportation', 'accommodation',
                    'food_dining', 'accessibility_essential_needs', 'crowds_atmosphere'):
            selected = getattr(self, key)
            if len(selected) != len(set(selected)):
                raise ValueError(f'Duplicate selection in {key}.')
        return self

class TravelProfileWrite(BaseModel):
    model_config = ConfigDict(extra='forbid')
    user_id: str = Field(default='local-demo', pattern=r'^[A-Za-z0-9_-]{1,80}$')
    consent: Literal[True]
    profile: TravelProfile

class TravelProfileReset(BaseModel):
    model_config = ConfigDict(extra='forbid')
    user_id: str = Field(default='local-demo', pattern=r'^[A-Za-z0-9_-]{1,80}$')
    confirmed: Literal[True]


def read_travel_profile(store, user_id: str) -> dict:
    """Read just this user's global profile; no session or other user can leak into it."""
    with store._lock:
        row = store._connection.execute(
            'SELECT value,updated_at FROM preferences WHERE user_id=? AND scope=? '
            'AND session_id=? AND category=?',
            (user_id, 'global', '*', PROFILE_CATEGORY),
        ).fetchone()
    if row is None:
        return {'profile': TravelProfile().model_dump(), 'saved': False, 'updated_at': None}
    # Corruption must not be silently reinterpreted as a valid empty profile.
    profile = TravelProfile.model_validate(json.loads(row['value']))
    return {'profile': profile.model_dump(), 'saved': True, 'updated_at': row['updated_at']}


def write_travel_profile(store, user_id: str, profile: TravelProfile) -> dict:
    encoded = profile.model_dump_json()
    now = datetime.now(timezone.utc).isoformat()
    with store._lock, store._connection:
        row = store._connection.execute(
            'SELECT value,updated_at FROM preferences WHERE user_id=? AND scope=? '
            'AND session_id=? AND category=?',
            (user_id, 'global', '*', PROFILE_CATEGORY),
        ).fetchone()
        if row is not None and row['value'] == encoded:
            return {'profile': profile.model_dump(), 'saved': True,
                    'updated_at': row['updated_at'], 'unchanged': True}
        store._connection.execute(
            'INSERT INTO preferences(user_id,scope,session_id,category,value,updated_at) '
            'VALUES(?,?,?,?,?,?) ON CONFLICT(user_id,scope,session_id,category) '
            'DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at',
            (user_id, 'global', '*', PROFILE_CATEGORY, encoded, now),
        )
    return {'profile': profile.model_dump(), 'saved': True,
            'updated_at': now, 'unchanged': False}


def reset_travel_profile(store, user_id: str) -> dict:
    with store._lock, store._connection:
        store._connection.execute(
            'DELETE FROM preferences WHERE user_id=? AND scope=? '
            'AND session_id=? AND category=?',
            (user_id, 'global', '*', PROFILE_CATEGORY),
        )
    return {'profile': TravelProfile().model_dump(), 'saved': False, 'updated_at': None}


def planner_preferences(raw_preferences: dict) -> dict:
    """Convert enum-only structured preferences into model-readable planning defaults.

    Existing preferences remain intact, but the JSON storage row is replaced in the
    LLM context with an explicit, bounded English summary. Session constraints and
    the current user request retain priority over these soft profile defaults.
    """
    if PROFILE_CATEGORY not in raw_preferences:
        return raw_preferences
    copy = dict(raw_preferences)
    value = copy.pop(PROFILE_CATEGORY)
    try:
        data = TravelProfile.model_validate(json.loads(value['value']))
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        # Malformed storage must not become a prompt injection route.
        return copy
    obj = data.model_dump()
    selected = {
        key.replace('_', ' ').title():
        ', '.join(item.replace('_', ' ') for item in val) if isinstance(val, list)
        else val.replace('_', ' ')
        for key, val in obj.items() if val and val != 'no_preference'
    }
    if selected:
        copy['travel_profile'] = {
            'value': json.dumps({
                'planning_defaults': selected,
                'priority': ('Current traveler request and trip-specific constraints override profile defaults. ' 'The structured profile supersedes conflicting older general saved preferences.'),
                'accessibility': ('Respect selected accessibility needs when recommending places. '
                                  'Do not claim accessibility is verified without provider evidence.'),
                'dietary': ('Treat selected dietary categories as traveler requirements when relevant. '
                            'Verify food suitability with a reliable source rather than assuming it.'),
                'provenance': 'Traveler-selected profile, not verified travel-provider information.',
            }, ensure_ascii=True),
            'scope': 'global',
        }
    return copy
