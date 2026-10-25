"""Verify the itinerary-only catalog; legacy utilities must not be active."""
from __future__ import annotations
from pathlib import Path
from services.registry import SkillRegistry
from services.itinerary_skill_scope import ACTIVE_ITINERARY_SKILLS


def verify(root: Path | None = None) -> tuple[int, int]:
    root = root or Path(__file__).resolve().parents[1]
    from core.config import settings
    expected_directory = (root / 'plugins' / 'skills').resolve()
    configured_directory = Path(settings.extensions_dir).resolve()
    if root.resolve() == Path(__file__).resolve().parents[1] and configured_directory != expected_directory:
        raise RuntimeError(f'Runtime Skill directory {configured_directory} differs from the migrated '
                           f'catalog at {expected_directory}. Update POREIAGO_SKILLS_DIRECTORY first.')
    registry = SkillRegistry(root / 'plugins' / 'skills')
    actual = frozenset(registry.specs)
    if actual != ACTIVE_ITINERARY_SKILLS:
        raise RuntimeError(f'Itinerary catalog mismatch; missing={sorted(ACTIVE_ITINERARY_SKILLS - actual)}, '
                           f'extra={sorted(actual - ACTIVE_ITINERARY_SKILLS)}')
    for name in sorted(ACTIVE_ITINERARY_SKILLS):
        spec = registry.get(name)
        directory = root / 'plugins' / 'skills' / name
        if spec.release_state != 'implemented' or spec.entrypoint != 'handler.py:run':
            raise RuntimeError(f'{name} must have its own executable handler.')
        for file in ('handler.py','manifest.json','SKILL.md',
                     'references/OPERATIONS.md','references/input_example.json'):
            if not (directory / file).is_file():
                raise RuntimeError(f'{name} missing {file}')
        if not list((directory / 'tests').glob('test_*.py')):
            raise RuntimeError(f'{name} missing independent tests')
    return len(ACTIVE_ITINERARY_SKILLS), len(actual)


if __name__ == '__main__':
    expected, installed = verify()
    print(f'Verified {installed}/{expected} itinerary-planning Skills.')
