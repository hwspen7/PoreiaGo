"""Manage running Aligo model endpoints through the authenticated backend API.

The CLI never edits the model configuration files directly; activation affects
new requests immediately, without disrupting active itinerary requests.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, HTTPRedirectHandler, build_opener


def _request(base_url: str, token: str, method: str, path: str, payload=None):
    endpoint = base_url.rstrip('/') + path
    try:
        parsed = urlsplit(base_url)
        port = parsed.port
    except ValueError:
        raise ValueError('The administration CLI accepts only local Aligo backend URLs.') from None
    if (parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', 'localhost', '::1')
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in ('', '/') or port is None):
        raise ValueError('The administration CLI accepts only local Aligo backend URLs.')
    headers = {'X-Aligo-Admin-Token': token}
    ordinary = os.environ.get('ALIGO_ACCESS_TOKEN', '')
    if ordinary:
        headers['Authorization'] = 'Bearer ' + ordinary
    body = None
    if payload is not None:
        headers['Content-Type'] = 'application/json'
        body = json.dumps(payload).encode('utf-8')
    request = Request(endpoint, data=body, method=method, headers=headers)
    class RefuseRedirect(HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    try:
        with build_opener(RefuseRedirect()).open(request, timeout=35) as response:
            return json.load(response)
    except HTTPError as exc:
        detail = 'Request rejected.'
        try:
            detail = json.load(exc).get('detail', detail)
        except (ValueError, AttributeError):
            pass
        raise RuntimeError(f'HTTP {exc.code}: {detail}') from None
    except URLError as exc:
        raise RuntimeError('Cannot connect to the local Aligo backend.') from None


def main(argv=None):
    parser = argparse.ArgumentParser(description='Manage the running Aligo model catalog.')
    parser.add_argument('--url', default='http://127.0.0.1:8000')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('list')
    add = sub.add_parser('add-profile')
    add.add_argument('--file', required=True, type=Path)
    remove = sub.add_parser('remove-profile')
    remove.add_argument('--name', required=True)
    preset = sub.add_parser('add-preset')
    preset.add_argument('--file', required=True, type=Path)
    activate = sub.add_parser('activate')
    activate.add_argument('--preset', required=True)
    activate.add_argument('--allow-remote', action='store_true')
    check = sub.add_parser('check')
    check.add_argument('--preset', required=True)
    check.add_argument('--stages', nargs='+', default=['daily'])
    check.add_argument('--connect', action='store_true')
    check.add_argument('--allow-remote', action='store_true')
    args = parser.parse_args(argv)
    token = os.environ.get('ALIGO_MODEL_ADMIN_TOKEN', '')
    if not token:
        parser.error('ALIGO_MODEL_ADMIN_TOKEN is required for model administration.')
    if args.command == 'list':
        result = _request(args.url, token, 'GET', '/api/models')
    elif args.command == 'add-profile':
        result = _request(args.url, token, 'POST', '/api/models/profiles',
                          json.loads(args.file.read_text(encoding='utf-8')))
    elif args.command == 'remove-profile':
        from urllib.parse import quote
        result = _request(args.url, token, 'DELETE', '/api/models/profiles/' +
                          quote(args.name, safe=''))
    elif args.command == 'add-preset':
        result = _request(args.url, token, 'POST', '/api/models/presets',
                          json.loads(args.file.read_text(encoding='utf-8')))
    elif args.command == 'activate':
        result = _request(args.url, token, 'POST', '/api/models/activate',
                          {'preset': None if args.preset == 'legacy' else args.preset,
                           'allow_remote': args.allow_remote})
    else:
        result = _request(args.url, token, 'POST', '/api/models/check',
                          {'preset': args.preset, 'stages': args.stages,
                           'connect': args.connect, 'allow_remote': args.allow_remote})
    print(json.dumps(result, indent=2, ensure_ascii=True))
    return result


if __name__ == '__main__':
    main()
