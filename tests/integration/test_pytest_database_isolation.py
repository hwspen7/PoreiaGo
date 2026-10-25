"""The test runner must not touch real PoreiaGo SQLite databases."""
from pathlib import Path
import os
from core.config import settings


def test_canonical_database_path_is_isolated_from_dev_environment():
    canonical = Path(os.environ["POREIAGO_DATABASE_PATH"]).resolve()
    legacy = Path(os.environ["ALIGO_DATABASE_PATH"]).resolve()
    assert canonical == legacy
    assert settings.db_path == canonical
    assert canonical.parent.name.startswith("poreiago-pytest-")
    assert canonical.name == "collection.sqlite3"
    from app.api import app
    assert Path(app.state.store.path).resolve() == canonical
