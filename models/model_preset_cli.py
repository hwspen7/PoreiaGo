"""Inspect and validate model-stage presets without making model requests."""
from __future__ import annotations

import argparse
from pathlib import Path

from core.config import settings
from intelligence.model_presets import list_presets
from intelligence.models import ModelGateway


def main():
    parser = argparse.ArgumentParser(description='Inspect model routing presets.')
    parser.add_argument('--list', action='store_true', help='List available presets.')
    parser.add_argument('--preset', help='Validate and display an effective model map.')
    args = parser.parse_args()
    path = Path(settings.models_file).parent / 'model_presets.json'
    if args.list or not args.preset:
        for name in list_presets(path):
            print(name)
    if args.preset:
        gateway = ModelGateway(settings, preset=args.preset)
        print('Model preset:', gateway.preset_name)
        for stage, info in gateway.selected_profiles().items():
            print(f'{stage}: {info["profile"]} -> {info["model"]} ({info["provider"]})')
        print('No model or provider requests were made.')


if __name__ == '__main__':
    main()
