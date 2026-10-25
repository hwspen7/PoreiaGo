"""Isolate the test suite from the development server's SQLite database.

Pytest loads root conftest.py before collecting tests. Since app.api creates
its default ASGI app at import time, the database path must be set before that
import occurs. This is test-only isolation: the production process guard is
unchanged and must continue rejecting competing API workers.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

_TEST_ROOT = Path(tempfile.mkdtemp(prefix="poreiago-pytest-"))
_TEST_DATABASE = _TEST_ROOT / "collection.sqlite3"
# Set both names before app imports. The canonical key MUST take precedence over
# any developer .env or inherited shell settings, preventing tests from opening
# the user's real SQLite database.
os.environ["POREIAGO_DATABASE_PATH"] = str(_TEST_DATABASE)
os.environ["ALIGO_DATABASE_PATH"] = str(_TEST_DATABASE)


def pytest_unconfigure(config):
    """Release the test-only app/lock before removing its temporary database."""
    api_module = sys.modules.get("app.api")
    app = getattr(api_module, "app", None)
    store = getattr(getattr(app, "state", None), "store", None)
    if store is not None:
        try:
            store.close()
        except Exception:
            # Do not mask the actual pytest result during interpreter shutdown.
            pass

    security_module = sys.modules.get("core.security")
    locks = getattr(security_module, "_DATABASE_LOCKS", None)
    if isinstance(locks, dict):
        owner = locks.get(str(_TEST_DATABASE.resolve()))
        if owner is not None and owner[0] == os.getpid():
            locks.pop(str(_TEST_DATABASE.resolve()), None)
            try:
                owner[1].close()
            except OSError:
                pass

    shutil.rmtree(_TEST_ROOT, ignore_errors=True)
