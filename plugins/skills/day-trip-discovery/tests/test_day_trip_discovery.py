"""Independent acceptance for day-trip-discovery."""
import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from services.registry import SkillRegistry


@pytest.mark.asyncio
async def test_documented_example():
    root = Path(__file__).resolve().parents[4]
    folder = root / 'plugins' / 'skills'
    args = json.loads((folder / 'day-trip-discovery' / 'references' / 'input_example.json').read_text(encoding='utf-8'))
    result = await SkillRegistry(folder).run('day-trip-discovery', SimpleNamespace(fields={}, providers=None), args)
    assert result.status in {'partial', 'ok'}
    assert result.data
    assert result.limitations
