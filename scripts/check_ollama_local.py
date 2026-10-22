"""Check that the installed local Ollama model can answer and produce JSON."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

import httpx
from openai import AsyncOpenAI

LIGHT = 'qwen3.5:9b-q8_0'
HEAVY = 'qwen3.8:27b-q4_K_M'
BASE_URL = 'http://127.0.0.1:11434'


async def smoke(model: str, *, generate: bool):
    async with httpx.AsyncClient(timeout=10) as http:
        data = (await http.get(BASE_URL + '/api/tags')).json()
    installed = {row.get('name') for row in data.get('models', [])}
    if model not in installed:
        print(f'NOT INSTALLED: {model}')
        print('Run: ollama pull ' + model)
        return 2
    print('INSTALLED:', model)
    if not generate:
        return 0
    client = AsyncOpenAI(base_url=BASE_URL + '/v1', api_key='ollama', timeout=300, max_retries=0)
    result = await client.chat.completions.create(
        model=model,
        messages=[
            {'role': 'system', 'content': 'Respond with a JSON object only.'},
            {'role': 'user', 'content': 'Give a JSON object with destination Japan and days integer 5.'},
        ],
        response_format={'type': 'json_object'},
        temperature=0.2,
    )
    content = result.choices[0].message.content or ''
    parsed = json.loads(content)
    if not isinstance(parsed, dict) or not parsed.get('destination') or parsed.get('days') != 5:
        raise ValueError('Model did not return the requested JSON fields.')
    print('PASSED: OpenAI-compatible JSON response.')
    return 0


def main():
    parser = argparse.ArgumentParser(description='Check local Ollama models from Aligo.')
    parser.add_argument('--model', choices=('light', 'heavy'), default='heavy')
    parser.add_argument('--generate', action='store_true', help='Run a real local inference request.')
    args = parser.parse_args()
    try:
        raise SystemExit(asyncio.run(smoke(HEAVY if args.model == 'heavy' else LIGHT,
                                          generate=args.generate)))
    except Exception as exc:
        print('FAILED:', type(exc).__name__, str(exc)[:250])
        sys.exit(1)


if __name__ == '__main__':
    main()
