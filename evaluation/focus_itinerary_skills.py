"""Archive legacy Skills and activate the itinerary-only catalog.

Run from the repository root: python -m evaluation.focus_itinerary_skills
Always preserve retired directories. Refuse unknown Skill directories instead
of modifying user-added extensions. The operation is safe to repeat.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
from services.itinerary_skill_scope import ACTIVE_ITINERARY_SKILLS, RETIRED_UTILITY_SKILLS

ROOT = Path(__file__).resolve().parents[1]


def migrate(root: Path = ROOT, *, dry_run: bool = False) -> dict:
    root = Path(root)
    directory = root / 'plugins' / 'skills'
    archive = root / 'plugins' / 'retired-skills-itinerary-focus'
    existing = {p.name for p in directory.iterdir() if p.is_dir() and (p/'manifest.json').exists()}
    unknown = existing - ACTIVE_ITINERARY_SKILLS - RETIRED_UTILITY_SKILLS
    if unknown:
        raise RuntimeError('Unknown custom Skills require explicit review: ' + ', '.join(sorted(unknown)))
    missing = ACTIVE_ITINERARY_SKILLS - existing
    if missing:
        raise RuntimeError('Missing required itinerary Skills: ' + ', '.join(sorted(missing)))
    for name in ACTIVE_ITINERARY_SKILLS:
        folder = directory / name
        if not (folder/'handler.py').is_file() or not (folder/'SKILL.md').is_file():
            raise RuntimeError(f'Incomplete itinerary Skill: {name}')
        manifest = json.loads((folder/'manifest.json').read_text(encoding='utf-8'))
        if manifest.get('release_state') != 'implemented' or manifest.get('entrypoint') != 'handler.py:run':
            raise RuntimeError(f'Invalid itinerary Skill manifest: {name}')
    moved = sorted(existing & RETIRED_UTILITY_SKILLS)
    collisions = [name for name in moved if (archive/name).exists()]
    if collisions:
        raise RuntimeError('Retirement archive collision; not overwriting: ' + ', '.join(collisions))
    if not dry_run:
        # The old 18-Skill suite tests APIs that intentionally no longer exist.
        # Preserve the exact file outside the active test collection.
        legacy_test = root / 'tests' / 'test_skillpack.py'
        legacy_archive = root / 'legacy_skill_tests' / 'test_skillpack.py.legacy'
        if moved and legacy_test.exists():
            if legacy_archive.exists():
                raise RuntimeError(f'Historical test archive collision: {legacy_archive}')
            legacy_archive.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(legacy_test), str(legacy_archive))
        for name in moved:
            archive.mkdir(parents=True, exist_ok=True)
            shutil.move(str(directory/name), str(archive/name))
        remaining = {p.name for p in directory.iterdir() if p.is_dir() and (p/'manifest.json').exists()}
        if remaining != ACTIVE_ITINERARY_SKILLS:
            raise RuntimeError(f'Active catalog mismatch: {sorted(remaining)}')
    return {'active': len(ACTIVE_ITINERARY_SKILLS), 'archived_now': len(moved),
            'archive': str(archive), 'dry_run': dry_run}


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run',action='store_true')
    args=parser.parse_args()
    print(migrate(dry_run=args.dry_run))
