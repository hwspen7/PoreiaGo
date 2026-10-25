"""Locally executable acceptance check for this Skill."""
from pathlib import Path
import json
from types import SimpleNamespace
import pytest
from services.registry import SkillRegistry
from core.contracts import Evidence
from services.providers import ProviderResult

ROOT = Path(__file__).resolve().parents[4]
SKILL = "place-detail-verification"

class FixtureProviders:
    async def request(self, capability, **kwargs):
        if capability == "geocode":
            return ProviderResult({"results":[{"name":kwargs.get("place","Osaka"),
                "country":"United States" if kwargs.get("place")=="Springfield" else "Japan",
                "latitude":34.7,"longitude":135.5}]}, [Evidence(provider="fixture-geocoder")])
        if capability == "places":
            item={"name":"Fixture Place","osm_id":10,"latitude":34.7,"longitude":135.5,
                  "category":"ceramics","cuisine":"Japanese"}
            return ProviderResult({"results":[item]},[Evidence(provider="fixture-places")])
        if capability == "walking_route":
            return ProviderResult({"distance_m":1000,"duration_s":900},[Evidence(provider="fixture-walk")])
        if capability == "weather":
            return ProviderResult({"hourly":{"time":["2026-11-01T10:00"],
                "temperature_2m":[18],"precipitation_probability":[20]}},[Evidence(provider="fixture-weather")])
        raise AssertionError("Unexpected provider capability")

class FixtureStore:
    def __init__(self):self.records=[]
    def expenses(self, user):return [x for x in self.records if x['user']==user]
    def add_expense(self,user,session,amount,currency,category):
        self.records.append(dict(user=user,session=session,amount=amount,
                                 currency=currency,category=category))

@pytest.mark.asyncio
async def test_local_skill_execution_and_output_contract():
    registry = SkillRegistry(ROOT/'plugins/skills')
    folder = ROOT/'plugins/skills'/SKILL
    args = json.loads((folder/'references/input_example.json').read_text(encoding='utf-8'))
    if SKILL == 'expense-tracking':args['consent']=True
    context = SimpleNamespace(providers=FixtureProviders(),store=FixtureStore(),
        fields={'destination':'Osaka'},query='travel request',
        user_id='fixture',session_id='trip',shared={})
    result = await registry.run(SKILL,context,args)
    assert result.status in ('ok','partial')
    assert "matches" in result.data
    assert (folder/'handler.py').is_file()
    assert (folder/'manifest.json').is_file()
    assert len((folder/'SKILL.md').read_text().splitlines())>=40
