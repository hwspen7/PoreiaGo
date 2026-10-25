"""Opt-in model-only versus legacy nine and eighteen itinerary Skill benchmark.

The same local model, suite and cases are used for each mode. This script
never enables cloud inference; network-backed provider discovery is opt-in.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def summarize(report: dict) -> dict:
    rows = report.get('results') or []
    tokens = 0
    calls = 0
    for row in rows:
        for entry in row.get('role_calls') or []:
            usage = entry.get('usage') or {}
            count = usage.get('total_tokens')
            if isinstance(count, (int, float)) and not isinstance(count, bool):
                tokens += count
                calls += 1
    return {
        'cases': len(rows),
        'passed': sum(row.get('status') == 'PASS' for row in rows),
        'degraded': sum(row.get('status') == 'DEGRADED' for row in rows),
        'failed': sum(row.get('status') == 'FAIL' for row in rows),
        'average_seconds': round(sum(row.get('elapsed_seconds', 0) for row in rows)
                                 / len(rows), 2) if rows else None,
        'observed_model_tokens': tokens if calls else None,
        'reported_model_calls': calls,
        'average_content_proxy': (round(sum(row['content_quality_score'] for row in rows
                                            if isinstance(row.get('content_quality_score'), (int, float)))
                                        / sum(isinstance(row.get('content_quality_score'), (int, float))
                                              for row in rows), 2)
                                  if any(isinstance(row.get('content_quality_score'), (int, float))
                                         for row in rows) else None),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', help='Explicitly authorize local Ollama inference.')
    parser.add_argument('--model', default='gemma4:12b-it-qat')
    parser.add_argument('--case', action='append', required=True,
                        help='Use one or more stable case IDs, repeat this flag.')
    parser.add_argument('--repeats', type=int, default=1)
    parser.add_argument('--online', action='store_true', help='Opt in to external places providers.')
    parser.add_argument('--output-dir', type=Path, default=ROOT / 'reports' / 'mode_benchmark')
    args = parser.parse_args()
    if not args.live:
        parser.error('--live is required before invoking Ollama.')
    if not 1 <= args.repeats <= 10:
        parser.error('--repeats must be between 1 and 10.')
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    summary = {'model': args.model, 'case_ids': args.case,
               'repeats': args.repeats, 'online': args.online, 'modes': {}}
    for label in ('model_only', 'nine', 'eighteen'):
        destination = output / f'{label}.json'
        environment = dict(os.environ)
        environment['POREIAGO_ITINERARY_SKILL_MODE'] = label if label != 'model_only' else 'none'
        command = [sys.executable, '-m', 'evaluation.evaluate_travel_quality',
                   '--live', '--models', args.model, '--repeats', str(args.repeats),
                   '--mode', 'model' if label == 'model_only' else 'integrated',
                   '--output', str(destination)]
        for case in args.case:
            command += ['--case', case]
        if args.online and label != 'model_only':
            command.append('--online')
        execution = subprocess.run(command, cwd=ROOT, env=environment, check=False)
        if not destination.is_file():
            raise SystemExit(f'{label} benchmark failed before producing a report, '
                             f'exit code: {execution.returncode}')
        report = json.loads(destination.read_text(encoding='utf-8'))
        summary['modes'][label] = {'report': destination.name,
                                   'exit_code': execution.returncode,
                                   **summarize(report)}
    path = output / 'comparison.json'
    path.write_text(json.dumps(summary, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(summary, indent=2))
    print('Comparison report:', path)


if __name__ == '__main__':
    main()
