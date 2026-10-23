#!/usr/bin/env python3
"""Repair the protected_constraints contract without replacing project modules.

Run from the repository root: python fix_commit_constraint_interfaces.py
The script edits only function signatures and the necessary constraint checks.
It refuses unfamiliar shapes, restores missing constraint support, and leaves backups.
"""
from __future__ import annotations

import ast
import io
from pathlib import Path
import shutil
import sys
import tokenize
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parent


def index_for(source: str, line: int, column: int) -> int:
    rows = source.splitlines(keepends=True)
    return sum(len(row) for row in rows[:line - 1]) + column


def function(source: str, name: str) -> ast.FunctionDef:
    tree = ast.parse(source)
    functions = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name]
    if len(functions) != 1:
        raise ValueError(f"Expected exactly one top-level {name}() definition")
    return functions[0]


def signature_insertion(source: str, node: ast.FunctionDef) -> tuple[int, str] | None:
    names = [a.arg for a in node.args.posonlyargs + node.args.args + node.args.kwonlyargs]
    if node.args.vararg:
        names.append(node.args.vararg.arg)
    if node.args.kwarg:
        names.append(node.args.kwarg.arg)
    if 'protected_constraints' in names:
        return None
    if node.args.kwarg is not None:
        raise ValueError(f"Unexpected **kwargs in {node.name} signature")
    tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    started = False
    open_token = None
    depth = 0
    close_token = None
    for tok in tokens:
        if tok.start[0] < node.lineno:
            continue
        if not started:
            if tok.type == tokenize.NAME and tok.string == node.name:
                started = True
            continue
        if open_token is None:
            if tok.string == '(':
                open_token = tok
                depth = 1
            continue
        if tok.string == '(':
            depth += 1
        elif tok.string == ')':
            depth -= 1
            if depth == 0:
                close_token = tok
                break
    if not open_token or not close_token:
        raise ValueError(f"Cannot parse signature of {node.name}")
    start = index_for(source, *open_token.end)
    end = index_for(source, *close_token.start)
    interior = source[start:end].rstrip()
    sep = '' if not interior or interior.endswith(',') else ','
    if open_token.start[0] == close_token.start[0]:
        addition = sep + ' protected_constraints=None'
    else:
        addition = sep + '\n                 protected_constraints=None'
    return end, addition


def update_validation_gate(source: str) -> tuple[str, list[str]]:
    node = function(source, 'validate_for_commit')
    section = ast.get_source_segment(source, node) or ''
    if 'normalized' not in section or 'issues' not in section:
        raise ValueError('validate_for_commit structure not recognized; no changes applied')
    ifs = [n for n in node.body if isinstance(n, ast.If) and
           isinstance(n.test, ast.Name) and n.test.id == 'issues']
    if not ifs:
        raise ValueError('Expected a final `if issues:` gate in validate_for_commit')
    changes = []
    insertions = []
    sig = signature_insertion(source, node)
    if sig:
        insertions.append(sig)
        changes.append('Add protected_constraints to validate_for_commit signature')
    if 'unresolved_requirements(' not in section:
        branch = ifs[-1]
        indentation = ' ' * branch.col_offset
        snippet = (
            indentation + 'from .journey_constraints import unresolved_requirements\n'
            + indentation + 'for entry in unresolved_requirements(normalized, protected_constraints, intent):\n'
            + indentation + "    issues.append(GateIssue(entry['code'], detail=entry['detail']))\n"
        )
        insertions.append((index_for(source, branch.lineno, 0), snippet))
        changes.append('Enforce persisted traveler constraints before commit')
    for at, content in sorted(insertions, reverse=True):
        source = source[:at] + content + source[at:]
    compile(source, 'aligo/validation_gate.py', 'exec')
    final_node = function(source, 'validate_for_commit')
    assert 'protected_constraints' in [a.arg for a in final_node.args.kwonlyargs]
    assert 'unresolved_requirements(' in (ast.get_source_segment(source, final_node) or '')
    return source, changes


def update_working_drafts(source: str) -> tuple[str, list[str]]:
    node = function(source, 'review_draft')
    calls = [n for n in ast.walk(node) if isinstance(n, ast.Call) and
             isinstance(n.func, ast.Name) and n.func.id == 'validate_for_commit']
    if len(calls) != 1:
        raise ValueError('Expected one validate_for_commit() call in review_draft')
    call = calls[0]
    insertions = []
    changes = []
    sig = signature_insertion(source, node)
    if sig:
        insertions.append(sig)
        changes.append('Add protected_constraints to review_draft signature')
    if not any(k.arg == 'protected_constraints' for k in call.keywords):
        at = index_for(source, call.end_lineno, call.end_col_offset - 1)
        if source[at] != ')':
            raise ValueError('Cannot locate closing parenthesis of draft gate call')
        previous = source[index_for(source, call.lineno, call.col_offset):at].rstrip()
        sep = '' if previous.endswith(',') else ','
        insertions.append((at, sep + ' protected_constraints=protected_constraints'))
        changes.append('Pass protected_constraints from review_draft to commit gate')
    for at, content in sorted(insertions, reverse=True):
        source = source[:at] + content + source[at:]
    compile(source, 'aligo/working_drafts.py', 'exec')
    final_node = function(source, 'review_draft')
    assert 'protected_constraints' in [a.arg for a in final_node.args.kwonlyargs]
    new_calls = [n for n in ast.walk(final_node) if isinstance(n, ast.Call) and
                 isinstance(n.func, ast.Name) and n.func.id == 'validate_for_commit']
    assert any(k.arg == 'protected_constraints' for k in new_calls[0].keywords)
    return source, changes


def verify_requirements_module(source: str) -> None:
    tree = ast.parse(source)
    names = {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}
    required = {'unresolved_requirements', 'requirements_from_intent', '_matching_place'}
    if not required.issubset(names):
        raise ValueError('Journey constraints module is missing required functions: '
                         + ', '.join(sorted(required - names)))


def main() -> int:
    gate = ROOT / 'aligo' / 'validation_gate.py'
    drafts = ROOT / 'aligo' / 'working_drafts.py'
    requirements = ROOT / 'aligo' / 'journey_constraints.py'
    asset = ROOT / 'patch_assets' / 'journey_constraints.py'
    required_dependencies = [ROOT / 'aligo' / 'time_contract.py']
    for path in (gate, drafts, *required_dependencies):
        if not path.is_file():
            print(f'ERROR: Required project module is missing: {path}')
            return 2
    try:
        # Check everything before modifying the repository.
        if requirements.is_file():
            requirement_source = requirements.read_text(encoding='utf-8')
            create_requirements = False
        else:
            if not asset.is_file():
                raise ValueError('Missing bundled patch_assets/journey_constraints.py. '
                                 'Extract the complete patch ZIP into the repository root.')
            requirement_source = asset.read_text(encoding='utf-8')
            create_requirements = True
        verify_requirements_module(requirement_source)
        results = {}
        for path, updater in ((gate, update_validation_gate),
                              (drafts, update_working_drafts)):
            original = path.read_text(encoding='utf-8')
            updated, changes = updater(original)
            results[path] = (original, updated, changes)
    except (ValueError, SyntaxError, AssertionError, UnicodeDecodeError) as exc:
        print(f'ABORTED with no files changed: {exc}')
        return 2

    timestamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    # Keep backups of every existing file before the first write.
    pending = [(path, original, updated, changes)
               for path, (original, updated, changes) in results.items()
               if original != updated]
    try:
        for path, original, updated, changes in pending:
            backup = path.with_name(path.name + '.backup-' + timestamp)
            shutil.copy2(path, backup)
        for path, original, updated, changes in pending:
            path.write_text(updated, encoding='utf-8', newline='')
            print(f'PATCHED: {path.relative_to(ROOT)}')
            for change in changes:
                print(f'  - {change}')
            print(f'  Backup: {path.name}.backup-{timestamp}')
        if create_requirements:
            # This is a new support module, never an overwrite of user code.
            with requirements.open('x', encoding='utf-8', newline='') as output:
                output.write(requirement_source)
            print('CREATED: aligo/journey_constraints.py')
    except OSError as exc:
        print(f'ERROR: Cannot write patch: {exc}')
        print('Restore any changed files using their .backup-* copies if needed.')
        return 2
    for path, (original, updated, changes) in results.items():
        if original == updated:
            print(f'ALREADY COMPATIBLE: {path.relative_to(ROOT)}')
    print('Success. Run: python -m pytest -q tests')
    return 0


if __name__ == '__main__':
    sys.exit(main())
