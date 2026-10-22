"""Reproducibility metadata for model-comparison reports."""
from __future__ import annotations

from hashlib import sha256
from pathlib import Path
import json
import os


def file_digest(path: Path) -> str | None:
    path = Path(path)
    return sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def evaluation_manifest(root: Path, suite_path: Path, gateway=None) -> dict:
    """Capture file revisions and effective stage model IDs; never include API keys."""
    root = Path(root)
    targets = {
        'scenario_suite': Path(suite_path),
        'model_registry': root / 'config/models.json',
        'model_presets': root / 'config/model_presets.json',
        'trip_router': root / 'aligo/model_agent.py',
        'planner': root / 'aligo/segmented_planning.py',
        'model_gateway': root / 'aligo/models.py',
        'skill_catalog': root / 'plugins/skills/README.md',
        'quality_scoring': root / 'aligo/quality_evaluation.py',
        'trip_contract': root / 'aligo/trip_state.py',
        'travel_intelligence': root / 'aligo/travel_intelligence.py',
        'quality_review': root / 'aligo/traveler_review.py',
        'recovery': root / 'aligo/day_recovery.py',
    }
    fingerprints = {name: digest for name, path in targets.items()
                    if (digest := file_digest(path)) is not None}
    skill_root = root / 'plugins' / 'skills'
    if skill_root.is_dir():
        skill_files = sorted(path for path in skill_root.iterdir() if path.is_dir())
        for folder in skill_files:
            for basename in ('manifest.json', 'handler.py'):
                candidate = folder / basename
                if candidate.is_file():
                    fingerprints[f'skill/{folder.name}/{basename}'] = file_digest(candidate)

    selected = gateway.selected_profiles() if gateway is not None and hasattr(gateway, 'selected_profiles') else None
    manifest = {'manifest_version': 1, 'model_preset': getattr(gateway, 'preset_name', None),
                'selected_stages': selected, 'sha256': fingerprints,
                'runtime_parameters': {
                    'daily_parallelism': os.getenv('ALIGO_DAILY_PARALLELISM', '1'),
                    'local_model_override': os.getenv('ALIGO_LOCAL_MODEL') or None,
                },
                'fact_verification': 'not_guaranteed'}
    manifest['fingerprint'] = sha256(json.dumps(manifest, sort_keys=True,
                                              ensure_ascii=True).encode('utf-8')).hexdigest()
    return manifest
