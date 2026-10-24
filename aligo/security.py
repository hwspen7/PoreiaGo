"""Explicit local-development and single-owner remote API safety boundaries.

A shared bearer token cannot authenticate different people. Multi-tenant hosting
requires a real identity provider and server-side principal-to-resource binding.
"""
from __future__ import annotations

import hmac
import json
import os
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

_TRUTHY = ('1', 'true', 'yes')


def require_admin_token(request: Request, enable_name: str, token_name: str) -> None:
    enabled = os.getenv(enable_name, '').lower() in _TRUTHY
    secret = os.getenv(token_name, '')
    if not enabled or not secret:
        raise HTTPException(403, f'Administration requires {enable_name}=1 and {token_name}.')
    supplied = request.headers.get('X-Aligo-Admin-Token', '')
    if not hmac.compare_digest(supplied, secret):
        raise HTTPException(401, 'A valid administration token is required.')


def guard_worker_configuration() -> None:
    """Fail closed for commonly configured multiple-worker deployments.

    Explicit uvicorn --workers flags cannot be detected in-process; use the
    documented single-worker startup command until distributed locks exist.
    """
    for key in ('WEB_CONCURRENCY', 'UVICORN_WORKERS', 'ALIGO_WORKERS'):
        raw = os.getenv(key, '').strip()
        if raw:
            try:
                count = int(raw)
            except ValueError as exc:
                raise RuntimeError(f'{key} must be an integer.') from exc
            if count > 1:
                raise RuntimeError('Aligo session serialization is single-process. '
                                   f'{key}={count} is unsupported until distributed locking is implemented.')


async def remote_owner_check(request: Request):
    """Restrict explicitly enabled remote bearer access to one data owner.

    Prevent arbitrary user_id switching. This remains a single-owner service:
    possession of its shared bearer token grants access to the owner's data.
    """
    owner = os.getenv('ALIGO_REMOTE_OWNER_ID', 'local-demo')
    if not owner or not owner.replace('_', '').replace('-', '').isalnum() or len(owner) > 80:
        return JSONResponse({'detail': 'Invalid ALIGO_REMOTE_OWNER_ID.'}, status_code=503)
    scoped = []
    if 'user_id' in request.query_params:
        scoped.extend(request.query_params.getlist('user_id'))
    path = request.url.path
    if path.startswith('/api/users/') and path.endswith('/data'):
        scoped.append(path[len('/api/users/'):-len('/data')].strip('/'))
    if request.method in ('POST', 'PUT', 'PATCH') and path.startswith('/api/'):
        # A bounded read protects the server against large unauthenticated body
        # uploads, including unbounded custom Skill arguments.
        size_limit = 256 * 1024
        raw_length = request.headers.get('content-length', '')
        if raw_length:
            try:
                if int(raw_length) > size_limit:
                    return JSONResponse({'detail': 'Request body is too large.'}, status_code=413)
            except ValueError:
                return JSONResponse({'detail': 'Invalid Content-Length.'}, status_code=400)
        if request.headers.get('content-type', '').split(';')[0].strip() == 'application/json':
            data = await request.body()
            if len(data) > size_limit:
                return JSONResponse({'detail': 'Request body is too large.'}, status_code=413)
            try:
                obj = json.loads(data)
            except (ValueError, UnicodeDecodeError):
                return JSONResponse({'detail': 'Invalid JSON body.'}, status_code=400)
            if isinstance(obj, dict) and 'user_id' in obj:
                scoped.append(obj['user_id'])
    # Several endpoints silently default to local-demo when the owner is not
    # included. A nondefault remote owner must always supply its own identity.
    data_scoped = (path in ('/api/sessions', '/api/history', '/api/trips/active')
                   or path.startswith(('/api/preferences', '/api/travel-profile', '/api/knowledge',
                                       '/api/chat', '/api/users/'))
                   or (path.startswith('/api/skills/') and path.endswith('/run')))
    if data_scoped and not scoped and owner != 'local-demo':
        return JSONResponse({'detail': 'Explicit remote owner identity is required.'}, status_code=403)
    if any(value != owner for value in scoped):
        return JSONResponse({'detail': 'Remote shared-token access is limited to its configured owner.'}, status_code=403)
    return None

# A lock held for the lifetime of the interpreter provides a final safeguard
# against unconfigured multi-process API workers on the same database file.
_DATABASE_LOCKS: dict[str, tuple[int, object]] = {}


def guard_database_process(database_path) -> None:
    """Disallow concurrent Aligo processes sharing one SQLite database.

    This complements the environment guard, which cannot see an explicit
    uvicorn --workers argument. No lock is needed for an in-memory test DB.
    """
    from pathlib import Path
    path = str(database_path)
    if path == ':memory:':
        return
    canonical = str(Path(path).resolve())
    current_pid = os.getpid()
    prior = _DATABASE_LOCKS.get(canonical)
    if prior and prior[0] == current_pid:
        return
    # Close any file descriptor inherited from a parent, then acquire in this
    # process. The child must not treat its parent's lock as authorization.
    if prior:
        try:
            prior[1].close()
        except OSError:
            pass
        _DATABASE_LOCKS.pop(canonical, None)
    lock_path = Path(canonical + '.aligo-server.lock')
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open('a+b')
    try:
        handle.seek(0)
        handle.write(b'1')
        handle.flush()
        handle.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, BlockingIOError) as exc:
        handle.close()
        raise RuntimeError('Aligo SQLite database is already owned by another '
                           'process. Run a single API worker or use a database '
                           'and distributed-lock architecture for multiworker hosting.') from exc
    _DATABASE_LOCKS[canonical] = (current_pid, handle)
