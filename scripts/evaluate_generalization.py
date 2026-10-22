"""Run offline, catalog-based travel intent coverage evaluation."""
import json
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from aligo.evaluation import evaluate_catalog, load_suite
from aligo.registry import SkillRegistry

if __name__ == '__main__':
    catalog = SkillRegistry(ROOT / 'plugins/skills')
    result = evaluate_catalog(catalog, load_suite(ROOT / 'evaluations/catalog_v1.json'))
    print(json.dumps(result, indent=2, ensure_ascii=False))
    raise SystemExit(0 if result['passed'] == result['cases'] else 1)
