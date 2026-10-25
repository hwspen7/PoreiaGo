"""Independent acceptance for local-events-discovery."""
import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from services.registry import SkillRegistry


@pytest.mark.asyncio
async def test_documented_example():
    root = Path(__file__).resolve().parents[4]
    folder = root / 'plugins' / 'skills'
    args = json.loads((folder / 'local-events-discovery' / 'references' / 'input_example.json').read_text(encoding='utf-8'))
    result = await SkillRegistry(folder).run('local-events-discovery', SimpleNamespace(fields={}, providers=None), args)
    assert result.status in {'partial', 'ok'}
    assert result.data
    assert result.limitations
