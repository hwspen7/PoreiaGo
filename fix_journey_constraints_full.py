#!/usr/bin/env python3
"""Apply the complete PoreiaGo journey-constraint patch safely.

Run in the repository root after extracting the complete patch archive. The
installer checks supported code shapes, prepares every edit in memory, creates
backups, and writes without overwriting unrelated UI or API modules.
"""
from __future__ import annotations

import argparse
import ast
from datetime import datetime, timezone
from pathlib import Path
import shutil
import sys

from patch_assets.v2_contract import (update_validation_gate, update_working_drafts,
                                      signature_insertion, function)


def replace_once(source: str, old: str, new: str, label: str) -> str:
    count = source.count(old)
    if count == 0 and source.count(new) == 1:
        return source  # Reapplying a previously completed patch is safe.
    if count != 1:
        raise ValueError(f'{label}: expected one exact insertion point; found {count}')
    return source.replace(old, new, 1)


def method_segment(source: str, name: str) -> tuple[str, int, int]:
    tree = ast.parse(source)
    cls = [node for node in tree.body if isinstance(node, ast.ClassDef)
           and node.name == 'MemoryStore']
    if len(cls) != 1:
        raise ValueError('Expected exactly one MemoryStore class')
    nodes = [node for node in cls[0].body if isinstance(node, ast.FunctionDef)
             and node.name == name]
    if len(nodes) != 1:
        raise ValueError(f'Expected one MemoryStore.{name} method')
    node = nodes[0]
    lines = source.splitlines(keepends=True)
    start = sum(len(row) for row in lines[:node.lineno - 1])
    end = sum(len(row) for row in lines[:node.end_lineno])
    return source[start:end], start, end


def edit_method(source: str, name: str, updates: list[tuple[str, str]]) -> str:
    segment, start, end = method_segment(source, name)
    for old, new in updates:
        segment = replace_once(segment, old, new, f'{name}')
    return source[:start] + segment + source[end:]


def update_gate(src: str) -> str:
    src, _ = update_validation_gate(src)
    node = function(src, 'validate_for_commit')
    args = [a.arg for a in node.args.kwonlyargs]
    if 'constraint_intents' not in args:
        src = replace_once(src, 'protected_constraints=None) -> dict:',
                           'protected_constraints=None, constraint_intents=None) -> dict:',
                           'commit gate signature')
    src = replace_once(src,
        'unresolved_requirements(normalized, protected_constraints, intent)',
        'unresolved_requirements(normalized, protected_constraints, intent,\n'
        '                                         superseding_intents=constraint_intents)',
        'validation_gate forward staged intents')
    compile(src, 'validation_gate.py', 'exec')
    return src


def update_drafts(src: str) -> str:
    src, _ = update_working_drafts(src)
    node = function(src, 'review_draft')
    if 'constraint_intents' not in [a.arg for a in node.args.kwonlyargs]:
        src = replace_once(src, 'protected_constraints=None):',
                           'protected_constraints=None, constraint_intents=None):',
                           'draft gate signature')
    src = replace_once(src,
        'protected_constraints=protected_constraints)',
        'protected_constraints=protected_constraints,\n'
        '                            constraint_intents=constraint_intents)',
        'review_draft forward staged intents')
    compile(src, 'working_drafts.py', 'exec')
    return src


def update_memory(src: str) -> str:
    if 'CREATE TABLE IF NOT EXISTS working_constraint_intents (' in src:
        indicators = ('def _pending_constraint_intents(', 'def restore_itinerary_version(',
                      'constraint_intents=None):', 'next_constraints = reconcile_requirements(')
        if not all(marker in src for marker in indicators):
            raise ValueError('Partial memory patch detected; restore backup before retrying')
        return src
    src = replace_once(src,
        '''                CREATE TABLE IF NOT EXISTS documents (''',
        '''                CREATE TABLE IF NOT EXISTS working_constraint_intents (
                    user_id TEXT NOT NULL, session_id TEXT NOT NULL,
                    draft_version INTEGER NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY(user_id,session_id,draft_version)
                );
                CREATE TABLE IF NOT EXISTS documents (''',
        'draft-intents table migration')

    src = edit_method(src, 'remember_journey_constraints', [(
'''        from .journey_constraints import requirements_from_intent
        requirements = requirements_from_intent(intent, document)
        removing = str((intent or {}).get('remove_place') or '').casefold().strip()
        if not requirements and not removing:
            return
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connection:
            if removing:
                from .journey_constraints import _matching_place
                rows = self._connection.execute(
                    'SELECT requirement_key,payload FROM journey_constraints '
                    'WHERE user_id=? AND session_id=?', (user_id, session_id)).fetchall()
                for row in rows:
                    constraint = json.loads(row['payload'])
                    if constraint.get('kind') != 'anchor':
                        continue
                    place = str(constraint.get('place') or '').casefold().strip()
                    if (place and (place == removing or place in removing or removing in place)
                            and not any(_matching_place(a, place)
                                for d in document.get('days', []) for a in d.get('activities', []))):
                        self._connection.execute(
                            'DELETE FROM journey_constraints WHERE user_id=? '
                            'AND session_id=? AND requirement_key=?',
                            (user_id, session_id, row['requirement_key']))
            for requirement in requirements:
                self._connection.execute(
                    'INSERT OR REPLACE INTO journey_constraints VALUES(?,?,?,?,?)',
                    (user_id, session_id, requirement['key'],
                     json.dumps(requirement, ensure_ascii=False), now))''',
'''        from .journey_constraints import reconcile_requirements
        if not isinstance(intent, dict):
            return
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connection:
            rows = self._connection.execute(
                'SELECT payload FROM journey_constraints WHERE user_id=? AND session_id=?',
                (user_id, session_id)).fetchall()
            existing = [json.loads(row['payload']) for row in rows]
            updated = reconcile_requirements(existing, [intent], document)
            self._connection.execute(
                'DELETE FROM journey_constraints WHERE user_id=? AND session_id=?',
                (user_id, session_id))
            for requirement in updated:
                self._connection.execute(
                    'INSERT INTO journey_constraints VALUES(?,?,?,?,?)',
                    (user_id, session_id, requirement['key'],
                     json.dumps(requirement, ensure_ascii=False), now))''')])

    # Append the read helper just before the draft writer.
    helper = '''    def _pending_constraint_intents(self, user_id, session_id, draft_version):
        if not draft_version:
            return []
        with self._lock:
            row = self._connection.execute(
                'SELECT payload FROM working_constraint_intents '
                'WHERE user_id=? AND session_id=? AND draft_version=?',
                (user_id, session_id, draft_version)).fetchone()
        result = json.loads(row['payload']) if row else []
        return [intent for intent in result if isinstance(intent, dict)]

'''
    if 'def _pending_constraint_intents(' not in src:
        anchor = '    def save_working_itinerary('
        if src.count(anchor) != 1:
            raise ValueError('Cannot locate insertion before save_working_itinerary')
        src = src.replace(anchor, helper + anchor, 1)

    src = edit_method(src, 'save_working_itinerary', [(
'''        candidate, current_issues = review_draft(
            document, previous=previous, intent=intent,
            protected_constraints=self.journey_constraints(user_id, session_id))''',
'''        prior_intents = self._pending_constraint_intents(user_id, session_id,
                                                         expected_draft_version)
        from .journey_constraints import compact_intents
        pending_intents = compact_intents(prior_intents + ([intent] if isinstance(intent, dict) and any(
            intent.get(name) is not None for name in
            ('anchor_place', 'early_topic', 'not_before_minute', 'remove_place')) else []))
        candidate, current_issues = review_draft(
            document, previous=previous, intent=intent,
            protected_constraints=self.journey_constraints(user_id, session_id),
            constraint_intents=pending_intents)
        from .validation_gate import _intent_issues
        accepted = self.active_itinerary(user_id, session_id)
        baseline = accepted['itinerary'] if accepted else None
        for pending in pending_intents:
            current_issues.extend(issue.as_dict() for issue in
                                  _intent_issues(candidate, baseline, pending))'''),(
'''            self._connection.execute(
                'INSERT INTO working_itinerary_versions VALUES(?,?,?,?,?,?,?)',
                (user_id, session_id, new_version, base_version,
                 json.dumps(candidate, ensure_ascii=False),
                 json.dumps(clean_issues, ensure_ascii=False, default=str), now))''',
'''            self._connection.execute(
                'INSERT INTO working_itinerary_versions VALUES(?,?,?,?,?,?,?)',
                (user_id, session_id, new_version, base_version,
                 json.dumps(candidate, ensure_ascii=False),
                 json.dumps(clean_issues, ensure_ascii=False, default=str), now))
            self._connection.execute(
                'INSERT INTO working_constraint_intents VALUES(?,?,?,?)',
                (user_id, session_id, new_version,
                 json.dumps(pending_intents, ensure_ascii=False)))''')])

    src = edit_method(src, 'save_current_itinerary', [(
'''        candidate, issues = review_draft(draft['itinerary'])''',
'''        pending_intents = self._pending_constraint_intents(
            user_id, session_id, draft['draft_version'])
        candidate, issues = review_draft(
            draft['itinerary'],
            protected_constraints=self.journey_constraints(user_id, session_id),
            constraint_intents=pending_intents)
        from .validation_gate import _intent_issues
        for pending in pending_intents:
            issues.extend(issue.as_dict() for issue in
                          _intent_issues(candidate, active['itinerary'] if active else None,
                                         pending))'''), (
'''            operation='revise' if active else 'create')''',
'''            operation='revise' if active else 'create',
            constraint_intents=pending_intents)''')])

    src = edit_method(src, 'save_itinerary', [(
'''                       operation=None, intent=None, trusted_sources=None, expected_draft_version=None):''',
'''                       operation=None, intent=None, trusted_sources=None, expected_draft_version=None,
                       constraint_intents=None):'''), (
'''            document = validate_for_commit(
                document, previous=intent_previous if effective == 'revise' else None,
                intent=intent, trusted_sources=trusted_sources,
                protected_constraints=self.journey_constraints(user_id, session_id))''',
'''            from .journey_constraints import reconcile_requirements
            rows = self._connection.execute(
                'SELECT payload FROM journey_constraints WHERE user_id=? AND session_id=?',
                (user_id, session_id)).fetchall()
            protected = ([json.loads(row['payload']) for row in rows]
                         if effective == 'revise' else [])
            staged = [entry for entry in (constraint_intents or []) if isinstance(entry, dict)]
            document = validate_for_commit(
                document, previous=intent_previous if effective == 'revise' else None,
                intent=intent, trusted_sources=trusted_sources,
                protected_constraints=protected, constraint_intents=staged)
            # Evaluate every persisted draft request against the accepted base.
            # A clean later draft must not silently discard an earlier request.
            for pending in staged:
                validate_for_commit(document, previous=prior, intent=pending,
                                    protected_constraints=protected,
                                    constraint_intents=staged)
            next_constraints = reconcile_requirements(
                protected, staged + ([intent] if isinstance(intent, dict) else []),
                document, previous=prior)'''), (
'''            self._connection.execute(
                'DELETE FROM working_itineraries WHERE user_id=? AND session_id=?',
                (user_id, session_id))''',
'''            self._connection.execute(
                'DELETE FROM working_itineraries WHERE user_id=? AND session_id=?',
                (user_id, session_id))
            self._connection.execute(
                'DELETE FROM journey_constraints WHERE user_id=? AND session_id=?',
                (user_id, session_id))
            for requirement in next_constraints:
                self._connection.execute(
                    'INSERT INTO journey_constraints VALUES(?,?,?,?,?)',
                    (user_id, session_id, requirement['key'],
                     json.dumps(requirement, ensure_ascii=False), now))''')])

    if 'def restore_itinerary_version(' not in src:
        anchor = '    def record_revision_diagnostic('
        method = '''    def restore_itinerary_version(self, user_id, session_id, version, *, expected_version):
        """Restore an accepted snapshot as a NEW version, never bypassing constraints."""
        snapshot = self.itinerary_version_document(user_id, session_id, version)
        if snapshot is None:
            raise ValueError('Requested itinerary version does not exist in this session.')
        return self.save_itinerary(user_id, session_id, snapshot,
                                   expected_version=expected_version, operation='revise')

'''
        if src.count(anchor) != 1:
            raise ValueError('Cannot locate revision diagnostic insertion point')
        src = src.replace(anchor, method + anchor, 1)
    src = replace_once(src,
        "'saved_trip_snapshots','journey_constraints'):",
        "'saved_trip_snapshots','journey_constraints','working_constraint_intents'):",
        'delete user removes sidecar intents')
    compile(src, 'memory.py', 'exec')
    return src


def update_runtime(src: str) -> str:
    first = "active_state['itinerary']"
    second = "draft_saved['itinerary']"
    if 'remember_journey_constraints(' not in src:
        return src
    if src.count('self.store.remember_journey_constraints(') != 2:
        raise ValueError('Partial or unrecognized runtime constraint hooks')
    # Constraints now commit atomically in MemoryStore.save_itinerary, so the
    # post-commit callback must not mutate state in an independent transaction.
    src = replace_once(src,
'''                        if operation == 'revise':
                            self.store.remember_journey_constraints(
                                user_id, session_id, explicit_revision_intent(query),
                                active_state['itinerary'])
''', '', 'runtime post-commit hook')
    src = replace_once(src,
'''                        if operation == 'revise':
                            self.store.remember_journey_constraints(
                                user_id, session_id, explicit_revision_intent(query),
                                draft_saved['itinerary'])
''', '', 'runtime provisional constraint hook')
    compile(src, 'runtime.py', 'exec')
    return src


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', default='.', help='Repository root (default: current directory)')
    parser.add_argument('--check', action='store_true', help='Validate patch compatibility, change nothing')
    args = parser.parse_args()
    root = Path(args.root).resolve()
    assets = Path(__file__).resolve().parent / 'patch_assets'
    targets = {
        'aligo/validation_gate.py': update_gate,
        'aligo/working_drafts.py': update_drafts,
        'aligo/memory.py': update_memory,
        'aligo/runtime.py': update_runtime,
    }
    try:
        planned = {}
        for name, updater in targets.items():
            target = root / name
            if not target.is_file():
                raise ValueError(f'Missing required file: {name}')
            original = target.read_text(encoding='utf-8')
            updated = updater(original)
            planned[target] = (original, updated)
        constraint_asset = (assets / 'journey_constraints.py').read_text(encoding='utf-8')
        compile(constraint_asset, 'journey_constraints.py', 'exec')
        candidate = root / 'aligo' / 'journey_constraints.py'
        if candidate.exists():
            old_constraint = candidate.read_text(encoding='utf-8')
            if 'def unresolved_requirements(' not in old_constraint or 'def requirements_from_intent(' not in old_constraint:
                raise ValueError('Unrecognized journey_constraints.py; no files modified')
        else:
            old_constraint = None
        planned[candidate] = (old_constraint, constraint_asset)
    except (ValueError, SyntaxError, OSError, AssertionError) as exc:
        print(f'ABORTED; no source files changed: {exc}')
        return 2
    changes = [item for item, (before, after) in planned.items() if before != after]
    print(f'Compatible source: {len(changes)} file(s) need updates.')
    for item in changes:
        print('  ', item.relative_to(root))
    if args.check:
        return 0
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backups = {}
    try:
        for path in changes:
            if path.is_file():
                dest = path.with_name(path.name + '.backup-' + stamp)
                shutil.copy2(path, dest)
                backups[path] = dest
        for path in changes:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(planned[path][1], encoding='utf-8', newline='')
    except OSError as exc:
        for path in changes:
            if path in backups:
                shutil.copy2(backups[path], path)
            elif planned[path][0] is None and path.is_file():
                path.unlink()
        print(f'ERROR, original files restored: {exc}')
        return 2
    print('COMPLETE. Run: python -m pytest -q tests/test_journey_constraints.py')
    return 0


if __name__ == '__main__':
    sys.exit(main())
