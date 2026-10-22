"""Inspect a local pytest fixture without modifying application or test code.

Usage:
    python -m scripts.diagnose_transfer_buffer
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from aligo.pipeline_integrity import schedule_invariants
from aligo.segmented_planning import _segment_issues


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    fixture = root / 'tests' / 'test_planning_patch_v364.py'
    print(f'Test file: {fixture}')
    print(f'Test file exists: {fixture.is_file()}')
    if not fixture.is_file():
        return
    spec = importlib.util.spec_from_file_location('_local_planning_patch_v364', fixture)
    if spec is None or spec.loader is None:
        raise RuntimeError('Could not load local test fixture')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    day = getattr(module, 'day', None)
    if not callable(day):
        print('The test module has no day() fixture')
        return
    value = day()
    print('Fixture activities:')
    print(json.dumps(value.get('activities', []), ensure_ascii=True, indent=2))
    print('Before adding transfer_minutes:')
    print(json.dumps(schedule_invariants(value), indent=2))
    value['activities'][1]['transfer_minutes'] = 25
    print('After adding transfer_minutes=25:')
    print('Direct schedule invariants:')
    print(json.dumps(schedule_invariants(value), indent=2))
    print('Segment issues:')
    print(json.dumps(_segment_issues([value], [{'day_type': 'sightseeing'}],
                                     request='Plan a day'), indent=2))


if __name__ == '__main__':
    main()
