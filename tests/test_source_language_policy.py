"""Prevent accidental introduction of hard-coded Han characters into source files."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SUFFIXES = {'.py', '.ts', '.tsx', '.js', '.json', '.md', '.html', '.css', '.yaml', '.yml'}
EXCLUDED = {'.git', '.pytest_cache', 'node_modules', 'dist', '__pycache__'}


def test_repository_text_has_no_han_characters():
    violations = []
    for path in ROOT.rglob('*'):
        if not path.is_file() or path.suffix.lower() not in SUFFIXES:
            continue
        if any(part in EXCLUDED for part in path.relative_to(ROOT).parts):
            continue
        content = path.read_text(encoding='utf-8')
        if any(0x3400 <= ord(char) <= 0x9FFF or 0xF900 <= ord(char) <= 0xFAFF
               for char in content):
            violations.append(str(path.relative_to(ROOT)))
    assert not violations, 'Source text contains disallowed Han characters: ' + ', '.join(violations)
