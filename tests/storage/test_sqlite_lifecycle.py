"""Regression tests for SQLite resource cleanup on Windows and other platforms."""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from storage.memory import MemoryStore


ROOT = Path(__file__).resolve().parents[2]


def test_store_context_manager_closes_sqlite_before_directory_cleanup():
    with TemporaryDirectory() as folder:
        database = Path(folder) / "eval.sqlite3"
        with MemoryStore(database) as store:
            store.add_document("test-user", "Example", "Text to index")
            connection = store._connection
            assert database.exists()
        with pytest.raises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")
        # Windows would raise PermissionError here if the connection remained open.
    assert not database.exists()


def test_store_context_manager_closes_on_failure():
    with TemporaryDirectory() as folder:
        database = Path(folder) / "eval.sqlite3"
        with pytest.raises(RuntimeError, match="intentional test error"):
            with MemoryStore(database) as store:
                connection = store._connection
                raise RuntimeError("intentional test error")
        with pytest.raises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")
    assert not database.exists()


def test_lexical_benchmark_completes_and_writes_report(tmp_path):
    """Execute the real CLI to detect accidental regression in resource cleanup."""
    output = tmp_path / "retrieval-report.json"
    result = subprocess.run(
        [sys.executable, "-m", "evaluation.evaluate_retrieval", "--mode", "lexical",
         "--output", str(output)],
        cwd=ROOT, capture_output=True, text=True, timeout=60, check=False,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["mode"] == "lexical"
    assert report["metrics"]
