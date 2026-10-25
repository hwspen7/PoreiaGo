"""Keep the npm package version synchronized with pyproject.toml.

Use --check in CI, or run with no arguments to repair version drift.
"""
from __future__ import annotations
import argparse
import json
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def synchronize(*, check=False):
    expected = tomllib.loads((ROOT / 'pyproject.toml').read_text(encoding='utf-8'))['project']['version']
    paths = (ROOT / 'web' / 'package.json', ROOT / 'web' / 'package-lock.json')
    changed = []
    for path in paths:
        if not path.exists():
            continue
        data = json.loads(path.read_text(encoding='utf-8'))
        if data.get('version') != expected or (path.name == 'package-lock.json' and
                data.get('packages', {}).get('', {}).get('version') != expected):
            changed.append(str(path.relative_to(ROOT)))
            if not check:
                data['version'] = expected
                if path.name == 'package-lock.json':
                    data['packages']['']['version'] = expected
                path.write_text(json.dumps(data, ensure_ascii=True, indent=2) + '\n', encoding='utf-8')
    if changed and check:
        raise SystemExit('Version mismatch: ' + ', '.join(changed))
    print('Version synchronized:', expected)

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--check', action='store_true')
    synchronize(check=parser.parse_args().check)
