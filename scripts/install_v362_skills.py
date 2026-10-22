"""Migrate a V3.6.1 install to exactly 18 active self-contained Skills.

Safe by design: retired skills are moved to a sibling archive, never erased.
Re-running is idempotent. Run after extracting the patch over project root.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]
ACTIVE = frozenset({
    'geocoding','place-discovery','place-detail-verification','source-verification',
    'restaurant-discovery','cultural-experience-search','airport-transfer','public-transit-router',
    'walking-directions','multi-stop-route-optimizer','schedule-conflict-validation',
    'hotel-search','hourly-weather-forecast','travel-cost-estimation',
    'trip-budget-allocation','expense-tracking','disruption-replanning','itinerary-export',
})

def migrate(*, root=ROOT, check_only=False):
    skills = root/'plugins'/'skills'
    backup = root/'plugins'/'retired-skills-v361'
    for name in sorted(ACTIVE):
        folder = skills/name
        if not (folder/'manifest.json').is_file() or not (folder/'handler.py').is_file() or not (folder/'SKILL.md').is_file():
            raise RuntimeError(f'Missing required V3.6.2 Skill content: {name}. Extract patch first.')
        manifest = json.loads((folder/'manifest.json').read_text(encoding='utf-8'))
        if manifest.get('entrypoint') != 'handler.py:run' or manifest.get('release_state') != 'implemented':
            raise RuntimeError(f'Skill still has a legacy manifest: {name}. Extract patch first.')
    retired=[]
    for folder in sorted(skills.iterdir()):
        if not folder.is_dir() or folder.name in ACTIVE or not (folder/'manifest.json').is_file():continue
        retired.append(folder.name)
        if not check_only:
            backup.mkdir(parents=True,exist_ok=True)
            destination=backup/folder.name
            if destination.exists():
                raise RuntimeError(f'Archive collision: {destination}. Resolve manually to protect previous data.')
            shutil.move(str(folder),str(destination))
    registered={p.parent.name for p in skills.glob('*/manifest.json')}
    if not check_only and registered!=ACTIVE:
        raise RuntimeError(f'Expected 18 Skills, found {len(registered)}: {sorted(registered^ACTIVE)}')
    return {'active':len(registered) if not check_only else len(ACTIVE),
            'retired_now':len(retired),'backup':str(backup)}

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check',action='store_true',help='Validate files and print retirement plan without moving.')
    args=parser.parse_args()
    print(migrate(check_only=args.check))
