"""Connectivity and multilingual sanity check; never prints or stores credentials."""
import asyncio
import os
import sys
from aligo.config import settings
from aligo.models import ModelGateway, ModelUnavailable


async def main() -> int:
    models = ModelGateway(settings)
    if not os.getenv('GROQ_API_KEY'):
        print('NOT CONFIGURED: Add GROQ_API_KEY to the project .env file.')
        return 2
    router = [p for p in models.catalog() if p['role'] == 'router' and p['configured']]
    print('Configured model:', router[0]['model'] if router else 'NONE')
    try:
        response = await models.complete(
            'router', 'Reply with only a JSON object containing a short German travel greeting as "reply".',
            'Please welcome me to Aligo briefly in German.', json_mode=True)
    except ModelUnavailable as exc:
        print('FAILED: Cloud model did not respond. Error types:', str(exc))
        return 1
    if not isinstance(response, dict) or not isinstance(response.get('reply'), str):
        print('FAILED: Unexpected JSON response shape.')
        return 1
    print('PASSED: Groq model generated JSON and German text.')
    print('Sample:', response['reply'][:180])
    return 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
