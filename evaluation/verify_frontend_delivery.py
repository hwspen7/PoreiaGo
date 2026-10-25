"""Verify frontend/backend contract, with an optional real-model planning run.

No model request occurs unless --live is supplied. The live mode creates a
fresh disposable session and validates the same SSE contract used by web/src.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from services.itinerary_skill_scope import ACTIVE_ITINERARY_SKILLS

CORE_SKILLS = ACTIVE_ITINERARY_SKILLS


def _local_address(address: str) -> str:
    value = urlsplit(address)
    if (value.scheme not in ('http', 'https') or
            value.hostname not in ('localhost', '127.0.0.1', '::1') or
            value.username or value.password or value.query or value.fragment):
        raise ValueError('Only explicit localhost service URLs are accepted.')
    return address.rstrip('/')


def _events(lines):
    """Read event-stream frames, including multi-line data and CRLF framing."""
    kind = 'message'
    data = []
    for raw in lines:
        line = raw.rstrip('\r')
        if line == '':
            if data:
                try:
                    yield kind, json.loads('\n'.join(data))
                except json.JSONDecodeError as exc:
                    raise ValueError('The backend sent a malformed SSE JSON frame.') from exc
            kind, data = 'message', []
        elif line.startswith('event:'):
            kind = line[6:].strip()
        elif line.startswith('data:'):
            data.append(line[5:].lstrip(' '))
    if data:
        yield kind, json.loads('\n'.join(data))


def run(base_url: str, frontend_url: str, *, live: bool, user_id: str,
        days: int, timeout: float, token: str = '') -> dict:
    report = {'backend': base_url, 'frontend': frontend_url,
              'model_request_sent': bool(live), 'checks': {}, 'warnings': []}

    def check(name, ok, reason=''):
        report['checks'][name] = 'PASS' if ok else 'FAIL'
        if not ok and reason:
            report['warnings'].append(f'{name}: {reason}')
        return ok

    headers = {'Authorization': f'Bearer {token}'} if token else {}
    with httpx.Client(headers=headers, timeout=httpx.Timeout(timeout, connect=5.0),
                      follow_redirects=False) as backend:
        health_response = backend.get(base_url + '/api/health')
        health_response.raise_for_status()
        health = health_response.json()
        check('backend_health', health.get('status') == 'ok', 'Expected a healthy backend.')
        catalog = backend.get(base_url + '/api/skills')
        catalog.raise_for_status()
        names = {row.get('id') for row in catalog.json().get('skills', [])}
        check('itinerary_skill_catalog', names == CORE_SKILLS,
              f'Expected 18 itinerary Skills, found {len(names)}.')
        configured_roles = {row.get('role') for row in health.get('models', [])
                            if row.get('configured')}
        check('planning_models_configured',
              {'router', 'reasoning'} <= configured_roles,
              'The router or reasoning planner is not configured.')

        with httpx.Client(headers=headers, timeout=httpx.Timeout(15.0, connect=5.0),
                          follow_redirects=False) as frontend:
            home = frontend.get(frontend_url + '/')
            is_html = ('text/html' in home.headers.get('content-type', '') and
                       home.status_code == 200)
            check('frontend_html', is_html,
                  'No HTML was served. Build web/dist or start the Vite frontend.')
            if is_html:
                paths = re.findall(r'(?:src|href)=["\'](/(?:assets/[^"\']+|src/main\.tsx))["\']', home.text)
                check('frontend_entry', bool(paths), 'No frontend application entry was found.')
                if paths:
                    resource = frontend.get(frontend_url + paths[0])
                    check('frontend_asset', resource.status_code == 200,
                          'The frontend bundle or development entry is missing.')

        if live and all(value == 'PASS' for value in report['checks'].values()):
            session_id = 'web-smoke-' + uuid4().hex[:16]
            message = (f'Plan a {days}-day cultural itinerary in Columbus, Ohio, '
                       'with named attractions, local food, realistic pacing, '
                       'a short why for each stop, and a rain alternative for each day.')
            start = time.monotonic()
            events = []
            with backend.stream('POST', base_url + '/api/chat/stream', json={
                    'message': message, 'user_id': user_id,
                    'session_id': session_id, 'context': {}}) as response:
                response.raise_for_status()
                check('stream_content_type', 'text/event-stream' in response.headers.get('content-type', ''))
                for kind, content in _events(response.iter_lines()):
                    if kind != 'heartbeat':
                        events.append((kind, content))
                    if time.monotonic() - start > timeout:
                        raise TimeoutError('The frontend stream exceeded the overall live-test deadline.')
                    if kind in ('done', 'error'):
                        break
            done = [data for kind, data in events if kind == 'done']
            errors = [data for kind, data in events if kind == 'error']
            check('stream_plan_event', any(kind == 'plan' for kind, _ in events),
                  'No planning event was sent.')
            check('stream_done_event', len(done) == 1 and not errors,
                  'The stream ended without a successful terminal done event.')
            report['elapsed_seconds'] = round(time.monotonic() - start, 2)
            if done:
                payload = done[-1]
                if payload.get('model_diagnostic'):
                    report['model_diagnostic'] = payload['model_diagnostic']
                answer = payload.get('answer', '')
                active = payload.get('active_itinerary') or {}
                itinerary = active.get('itinerary') or {}
                actual_days = itinerary.get('days') or []
                check('visible_answer', isinstance(answer, str) and len(answer.strip()) >= 80,
                      'The frontend did not receive useful travel content.')
                check('structured_itinerary', len(actual_days) == days and
                      all(isinstance(day.get('activities'), list) and day['activities'] for day in actual_days),
                      f'Expected {days} nonempty itinerary days.')
                check('saved_trip_version', active.get('version') == 1 and
                      (payload.get('trip_change') or {}).get('action') == 'created',
                      'The new itinerary was not committed.')
                check('no_model_issue', not payload.get('model_issue'),
                      'The model reported a planning issue.')
                saved = backend.get(base_url + '/api/trips/active',
                                    params={'user_id': user_id, 'session_id': session_id})
                saved.raise_for_status()
                check('reload_saved_trip', saved.json().get('active_itinerary') == active,
                      'The frontend would not reload the same trip.')
                history = backend.get(base_url + '/api/history',
                                      params={'user_id': user_id, 'session_id': session_id})
                history.raise_for_status()
                chat = history.json().get('messages', [])
                check('reload_chat_history', bool(chat) and chat[-1].get('content') == answer,
                      'The frontend would not reload the answer.')
                report['summary'] = {'day_count': len(actual_days),
                                     'title': itinerary.get('title'),
                                     'answer_chars': len(answer), 'session_id': session_id}
            else:
                report['warnings'].append('No final itinerary was returned by the backend.')
        elif live:
            report['warnings'].append('Live inference was not started because preflight failed.')
    report['ok'] = bool(report['checks']) and all(
        result == 'PASS' for result in report['checks'].values())
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default='http://127.0.0.1:8000')
    parser.add_argument('--frontend-url', default=None)
    parser.add_argument('--live', action='store_true',
                        help='Send one real itinerary request through the frontend SSE endpoint.')
    parser.add_argument('--user-id', default='local-demo')
    parser.add_argument('--days', type=int, default=3)
    parser.add_argument('--timeout', type=float, default=600.)
    parser.add_argument('--report', help='Optional local path for a JSON diagnostics report.')
    args = parser.parse_args(argv)
    if not 1 <= args.days <= 7:
        parser.error('--days must be between 1 and 7 for a short smoke test.')
    try:
        base_url = _local_address(args.base_url)
        frontend_url = _local_address(args.frontend_url or args.base_url)
        result = run(base_url, frontend_url, live=args.live, user_id=args.user_id,
                     days=args.days, timeout=args.timeout,
                     token=os.getenv('POREIAGO_ACCESS_TOKEN', ''))
        for name, outcome in result['checks'].items():
            print(f'{outcome:4}  {name}')
        for warning in result['warnings']:
            print(f'NOTE  {warning}')
        if args.report:
            from pathlib import Path
            path = Path(args.report)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding='utf-8')
            print(f'Report: {path}')
        print('FRONTEND DELIVERY ' + ('PASS' if result['ok'] else 'FAIL'))
        return 0 if result['ok'] else 1
    except (httpx.HTTPError, TimeoutError, ValueError) as exc:
        print(f'FRONTEND DELIVERY FAIL: {type(exc).__name__}: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
