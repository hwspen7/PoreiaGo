"""Compatibility entrypoint for the current professional skill catalog."""
from pathlib import Path
import json

ROOT = Path(__file__).resolve().parents[1]
from aligo.itinerary_skill_scope import ACTIVE_ITINERARY_SKILLS
EXPECTED = ACTIVE_ITINERARY_SKILLS


def verify(root=ROOT):
    folder = Path(root) / 'plugins' / 'skills'
    actual = {path.name for path in folder.iterdir()
              if path.is_dir() and (path / 'manifest.json').exists()} if folder.is_dir() else set()
    if actual != EXPECTED:
        raise RuntimeError(f'Active skill directory mismatch at {folder}: '
                           f'missing={sorted(EXPECTED - actual)} extra={sorted(actual - EXPECTED)}')
    for name in sorted(EXPECTED):
        directory = folder / name
        manifest = json.loads((directory / 'manifest.json').read_text(encoding='utf-8'))
        if manifest.get('id') != name or manifest.get('release_state') != 'implemented' or \
                manifest.get('entrypoint') != 'handler.py:run':
            raise RuntimeError(f'Invalid active implementation contract: {name}')
        for required in ('handler.py', 'SKILL.md', 'references/OPERATIONS.md',
                         'references/input_example.json'):
            if not (directory / required).is_file():
                raise RuntimeError(f'Missing {name}/{required}')
        if not list((directory / 'tests').glob('test_*.py')):
            raise RuntimeError(f'Missing {name} specialist test')
        if 'Itinerary-first maturity behavior' not in (directory / 'SKILL.md').read_text(encoding='utf-8'):
            raise RuntimeError(f'Stale Skill manual for {name}')
    print(f'Validated {len(EXPECTED)}/{len(EXPECTED)} independently executable itinerary Skills at {folder}')
    return len(EXPECTED)


if __name__ == '__main__':
    verify()
