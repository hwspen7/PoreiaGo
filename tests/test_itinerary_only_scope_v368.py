"""Focused itinerary products must not depend on unrelated travel utilities."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from aligo.api import create_app
from aligo.itinerary_skill_scope import ACTIVE_ITINERARY_SKILLS, RETIRED_UTILITY_SKILLS
from aligo.registry import SkillRegistry
from aligo.segmented_planning import generate_daily_trip
from scripts.focus_itinerary_skills import migrate
from scripts.verify_v363_skills import verify

ROOT = Path(__file__).resolve().parents[1]


def test_only_eighteen_itinerary_skills_are_registered():
    registry = SkillRegistry(ROOT / 'plugins' / 'skills')
    assert set(registry.specs) == ACTIVE_ITINERARY_SKILLS
    assert not RETIRED_UTILITY_SKILLS.intersection(registry.specs)
    assert verify(ROOT) == (18, 18)


@pytest.mark.asyncio
async def test_retired_tool_endpoints_do_not_execute(tmp_path):
    app = create_app(database_path=tmp_path / 'scope.sqlite3')
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://testclient') as client:
        for name in sorted(RETIRED_UTILITY_SKILLS):
            response = await client.post(f'/api/skills/{name}/run', json={'arguments': {}})
            assert response.status_code == 404, name
        assert (await client.get('/api/skills')).json()['count'] == 18


def test_archival_is_idempotent_and_rejects_unknown_custom_skill(tmp_path):
    directory = tmp_path / 'plugins' / 'skills'
    for name in ACTIVE_ITINERARY_SKILLS:
        target = directory / name
        target.mkdir(parents=True)
        (target / 'handler.py').write_text('async def run(ctx,args): return None', encoding='utf-8')
        (target / 'SKILL.md').write_text('Valid example', encoding='utf-8')
        (target / 'manifest.json').write_text(json.dumps({
            'release_state': 'implemented', 'entrypoint': 'handler.py:run'}), encoding='utf-8')
    for name in ('airport-transfer', 'expense-tracking'):
        target = directory / name
        target.mkdir()
        (target / 'manifest.json').write_text('{}', encoding='utf-8')
    preview = migrate(tmp_path, dry_run=True)
    assert preview['archived_now'] == 2
    assert (directory / 'airport-transfer').exists()
    result = migrate(tmp_path)
    assert result['active'] == 18 and result['archived_now'] == 2
    assert (tmp_path / 'plugins/retired-skills-itinerary-focus/airport-transfer/manifest.json').exists()
    assert migrate(tmp_path)['archived_now'] == 0
    unknown = directory / 'custom-user-skill'
    unknown.mkdir()
    (unknown / 'manifest.json').write_text('{}', encoding='utf-8')
    with pytest.raises(RuntimeError, match='Unknown custom Skills'):
        migrate(tmp_path)


class RegionalModel:
    async def complete_global_blueprint(self, system, prompt, *, day_count, compact=False):
        assert day_count == 3
        return {'blueprint': {'destination': 'Kyoto', 'title': 'Kyoto and Nara heritage',
                'summary': 'Explore historic neighborhoods and local flavors.',
                'days': [
                    {'day': 1, 'city': 'Kyoto', 'area': 'Higashiyama', 'theme': 'Heritage',
                     'anchors': ['Kiyomizu-dera'], 'overnight': 'Kyoto', 'transfer': '',
                     'day_type': 'sightseeing'},
                    {'day': 2, 'city': 'Nara', 'area': 'Nara Park', 'theme': 'History',
                     'anchors': ['Todai-ji'], 'overnight': 'Kyoto', 'transfer': '',
                     'day_type': 'sightseeing'},
                    {'day': 3, 'city': 'Kyoto', 'area': 'Northern Kyoto', 'theme': 'Zen',
                     'anchors': ['Ryoan-ji'], 'overnight': 'Kyoto', 'transfer': '',
                     'day_type': 'sightseeing'},
                ]}}

    async def complete_daily_detail(self, system, prompt, *, day_number, compact=False):
        request = json.loads(prompt)
        assert request['day_number'] == day_number
        places = ['Kiyomizu-dera', 'Todai-ji', 'Ryoan-ji']
        description = ('Depart from Kyoto via the Kintetsu line and visit Todai-ji.'
                       if day_number == 2 else f'Explore {places[day_number-1]} and its gardens.')
        return {'days': [{'day': day_number, 'title': f'Culture in day {day_number}',
                'area_cluster': request['allocated_days'][0]['area'],
                'logistics': 'Walk or use a local train between cultural sites.',
                'weather_alternative': 'Explore a nearby museum instead.',
                'activities': [
                    {'description': description, 'time': 'Morning', 'duration_minutes': 90},
                    {'description': 'Sample local regional dishes for lunch.', 'time': 'Afternoon'},
                    {'description': f'Explore a different local neighborhood on day {day_number}.',
                     'time': 'Evening'}]}]}


@pytest.mark.asyncio
async def test_day_trip_focuses_on_sights_without_airport_gate():
    model = RegionalModel()
    plan = await generate_daily_trip(model, request='Plan three days around Kyoto and Nara heritage',
        destination='Kyoto', days=3, fields={'destination': 'Kyoto', 'days': 3},
        preferences={})
    assert len(plan['days']) == 3
    assert plan['days'][1]['activities'][0]['description'].startswith('Depart from Kyoto via')
    assert plan['days'][1]['overnight_base'] == 'Kyoto'
    assert not any('airport arrival' in str(v) for v in plan.get('predeparture_checks', []))
