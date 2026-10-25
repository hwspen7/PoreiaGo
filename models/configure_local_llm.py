"""Register local Ollama profiles without changing model credentials or existing profiles."""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCAL_ROLES = ('router', 'reasoning', 'synthesis')


def configure_models(path: Path, *, backup: bool = True) -> list[str]:
    """Prepend local profiles while preserving existing cloud and retrieval profiles."""
    payload = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(payload, dict) or not isinstance(payload.get('profiles'), list):
        raise ValueError('Model registry must contain a profiles list.')
    retained = [profile for profile in payload['profiles']
                if profile.get('name') not in {f'ollama-{role}' for role in LOCAL_ROLES}]
    local_profiles = [
        {
            'name': f'ollama-{role}',
            'role': role,
            'provider': 'openai-compatible',
            'model': 'qwen3.8:27b-q4_K_M',
            'model_env': 'POREIAGO_LOCAL_MODEL',
            'base_url': 'http://127.0.0.1:11434/v1',
            'api_key_env': 'POREIAGO_OLLAMA_API_KEY',
            'request_timeout_seconds': 300,
            # Small routing decisions; full itineraries are generated separately.
            'decision_only': role == 'router',
            # Routing is classification, not deliberation. Prevent thinking
            # tokens from exhausting the short JSON decision budget.
            **({'reasoning_effort': 'none'} if role == 'router' else {}),
            'max_output_tokens': {'router': 3072, 'reasoning': 8192, 'synthesis': 8192}[role],
        }
        for role in LOCAL_ROLES
    ]
    if backup:
        # Keep project configuration backups outside the config directory.
        default_registry = ROOT / 'config' / 'models.json'

        if path.resolve() == default_registry.resolve():
            backup_dir = ROOT / 'data' / 'backups'
        else:
            backup_dir = path.parent / 'backups'

        backup_dir.mkdir(parents=True, exist_ok=True)
        destination = backup_dir / 'models.before-ollama.json'

        if not destination.exists():
            shutil.copy2(path, destination)
    payload['profiles'] = local_profiles + retained
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return [p['name'] for p in local_profiles]


def main():
    parser = argparse.ArgumentParser(description='Enable local Ollama model profiles for PoreiaGo.')
    parser.add_argument('--models-file', type=Path, default=ROOT / 'config/models.json')
    args = parser.parse_args()
    names = configure_models(args.models_file)
    print('Registered local model profiles:', ', '.join(names))
    print('Router: decision_only=true, reasoning_effort=none, max_output_tokens=3072')
    print('Backup: data/backups/models.before-ollama.json (created if absent).')
    print('The .env file was not modified. Configure POREIAGO_OLLAMA_API_KEY and POREIAGO_LOCAL_MODEL.')
    print('Clear GROQ_API_KEY to prevent unintentional cloud fallback.')


if __name__ == '__main__':
    main()
