"""Deterministic catalog benchmarks; no external APIs or model weights required."""
from __future__ import annotations
import json
from dataclasses import dataclass
from pathlib import Path
from intelligence.intelligence import CatalogMatcher
from planning.planning import PlanCompiler
from core.runtime import extract_fields


@dataclass
class EvaluationResult:
    case_id: str
    passed: bool
    actual: list[str]
    expected: list[str]
    unexpected: list[str]
    notes: str


def evaluate_catalog(registry, cases, maximum=12):
    matcher = CatalogMatcher(registry)
    results = []
    for case in cases:
        fields = {**extract_fields(case['request']), **case.get('context', {})}
        candidates = matcher.rank(case['request'], fields, max_results=maximum)
        compiled = PlanCompiler(registry, maximum=maximum).compile(candidates)
        expected = case.get('expected_skills', [])
        forbidden = case.get('forbidden_skills', [])
        missing = [name for name in expected if name not in compiled.names]
        unexpected = [name for name in forbidden if name in compiled.names]
        results.append(EvaluationResult(case['id'], not missing and not unexpected,
                                        compiled.names, expected, unexpected,
                                        'Missing: ' + ', '.join(missing) if missing else 'All declared catalog expectations met.'))
    return {'cases':len(results), 'passed':sum(x.passed for x in results),
            'pass_rate':round(sum(x.passed for x in results)/max(1,len(results)),4),
            'results':[result.__dict__ for result in results],
            'limitations':['Lexical catalog selection is evaluated offline.',
                           'A passing result does not establish live provider accuracy or itinerary quality.',
                           'Multilingual intent interpretation requires an appropriately configured router model.']}


def load_suite(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))['cases']
