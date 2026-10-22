"""Audit a local JSON itinerary or a local quality-report JSON file, offline."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from aligo.content_audit import audit_itinerary


def audit_file(path: Path) -> list[dict]:
    content = json.loads(path.read_text(encoding='utf-8'))
    if isinstance(content, dict) and 'results' in content:
        rows = [{'label': str(item.get('case_id', 'unknown')),
                 'itinerary': item['itinerary'],
                 'request': item.get('request', ''),
                 'expected_days': item.get('fields', {}).get('days')}
                for item in content['results'] if isinstance(item.get('itinerary'), dict)]
    else:
        if not isinstance(content, dict):
            raise ValueError('Expected an itinerary object or quality report object.')
        trip = content.get('itinerary', content)
        rows = [{'label': path.stem, 'itinerary': trip,
                 'request': '', 'expected_days': None}]
    return [{'case': item['label'], **audit_itinerary(
        item['itinerary'], request=item['request'],
        expected_days=item['expected_days'])} for item in rows]


def main() -> None:
    parser = argparse.ArgumentParser(description='Audit saved travel content without model calls.')
    parser.add_argument('json_file', type=Path)
    parser.add_argument('--output', type=Path, default=None)
    args = parser.parse_args()
    results = audit_file(args.json_file)
    rendered = json.dumps({'audits': results}, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + '\n', encoding='utf-8')
        print(args.output)
    else:
        print(rendered)


if __name__ == '__main__':
    main()
