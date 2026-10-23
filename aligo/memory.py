"""SQLite storage: explicit user scopes, session history, and consent-gated preferences."""
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock

class MemoryStore:
    def __init__(self, path):
        self.path = str(path)
        self._lock = RLock()
        if self.path != ':memory:':
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        with self._connection:
            self._connection.executescript('''
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL, session_id TEXT NOT NULL,
                    role TEXT NOT NULL, content TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_messages_scope ON messages(user_id, session_id, id);
                CREATE TABLE IF NOT EXISTS preferences (
                    user_id TEXT NOT NULL, scope TEXT NOT NULL, session_id TEXT NOT NULL,
                    category TEXT NOT NULL, value TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(user_id, scope, session_id, category)
                );
                CREATE TABLE IF NOT EXISTS expenses (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL,
                    session_id TEXT NOT NULL, amount REAL NOT NULL,
                    currency TEXT NOT NULL, category TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS trips (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL,
                    title TEXT NOT NULL, summary TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS active_itineraries (
                    user_id TEXT NOT NULL, session_id TEXT NOT NULL,
                    version INTEGER NOT NULL, document TEXT NOT NULL, updated_at TEXT NOT NULL,
                    PRIMARY KEY (user_id, session_id)
                );
                CREATE TABLE IF NOT EXISTS itinerary_versions (
                    user_id TEXT NOT NULL, session_id TEXT NOT NULL,
                    version INTEGER NOT NULL, document TEXT NOT NULL, updated_at TEXT NOT NULL,
                    PRIMARY KEY (user_id, session_id, version)
                );
                CREATE TABLE IF NOT EXISTS working_itineraries (
                    user_id TEXT NOT NULL, session_id TEXT NOT NULL,
                    draft_version INTEGER NOT NULL, base_version INTEGER NOT NULL,
                    document TEXT NOT NULL, issues TEXT NOT NULL, updated_at TEXT NOT NULL,
                    PRIMARY KEY (user_id, session_id)
                );
                CREATE TABLE IF NOT EXISTS revision_diagnostics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL, session_id TEXT NOT NULL,
                    baseline_version INTEGER NOT NULL,
                    stage TEXT NOT NULL, details TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_revision_diagnostics_scope
                    ON revision_diagnostics(user_id,session_id,id);
                CREATE TABLE IF NOT EXISTS working_itinerary_versions (
                    user_id TEXT NOT NULL, session_id TEXT NOT NULL,
                    draft_version INTEGER NOT NULL, base_version INTEGER NOT NULL,
                    document TEXT NOT NULL, issues TEXT NOT NULL, updated_at TEXT NOT NULL,
                    PRIMARY KEY(user_id, session_id, draft_version)
                );
                INSERT OR IGNORE INTO working_itinerary_versions
                    SELECT * FROM working_itineraries;
                CREATE TABLE IF NOT EXISTS saved_trip_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL, session_id TEXT NOT NULL,
                    source_status TEXT NOT NULL, source_version INTEGER NOT NULL,
                    document TEXT NOT NULL, issues TEXT NOT NULL, saved_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_saved_snapshots_scope
                    ON saved_trip_snapshots(user_id,session_id,id);
                CREATE TABLE IF NOT EXISTS journey_constraints (
                    user_id TEXT NOT NULL, session_id TEXT NOT NULL,
                    requirement_key TEXT NOT NULL, payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (user_id, session_id, requirement_key)
                );
                CREATE TABLE IF NOT EXISTS working_constraint_intents (
                    user_id TEXT NOT NULL, session_id TEXT NOT NULL,
                    draft_version INTEGER NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY(user_id,session_id,draft_version)
                );
                CREATE TABLE IF NOT EXISTS documents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL, title TEXT NOT NULL, content TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
            ''')

    def close(self):
        """Release the SQLite file handle, including on Windows."""
        with self._lock:
            self._connection.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
        return False

    def active_itinerary(self, user_id, session_id):
        """Read the current trip only within its exact user/session scope."""
        with self._lock:
            row = self._connection.execute(
                'SELECT version,document,updated_at FROM active_itineraries WHERE user_id=? AND session_id=?',
                (user_id, session_id)).fetchone()
        if row is None:
            return None
        return {'version': row['version'], 'itinerary': json.loads(row['document']),
                'updated_at': row['updated_at']}

    def itinerary_versions(self, user_id, session_id):
        with self._lock:
            rows = self._connection.execute(
                'SELECT version,updated_at FROM itinerary_versions WHERE user_id=? AND session_id=? ORDER BY version DESC',
                (user_id, session_id)).fetchall()
        return [dict(row) for row in rows]

    def itinerary_version_document(self, user_id, session_id, version):
        """Read one historical itinerary within the exact user and session scope."""
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            return None
        with self._lock:
            row = self._connection.execute(
                'SELECT document FROM itinerary_versions '
                'WHERE user_id=? AND session_id=? AND version=?',
                (user_id, session_id, version)).fetchone()
        return json.loads(row['document']) if row else None

    def working_itinerary(self, user_id, session_id):
        """A session-local provisional snapshot, never a certified trip version."""
        with self._lock:
            row = self._connection.execute(
                'SELECT draft_version,base_version,document,issues,updated_at '
                'FROM working_itineraries WHERE user_id=? AND session_id=?',
                (user_id, session_id)).fetchone()
        if not row:
            return None
        return {'draft_version': row['draft_version'], 'base_version': row['base_version'],
                'itinerary': json.loads(row['document']), 'issues': json.loads(row['issues']),
                'updated_at': row['updated_at'], 'status': 'draft'}

    def journey_constraints(self, user_id, session_id):
        """Return only the user's explicitly established session constraints."""
        with self._lock:
            rows = self._connection.execute(
                'SELECT payload FROM journey_constraints WHERE user_id=? AND session_id=? '
                'ORDER BY requirement_key', (user_id, session_id)).fetchall()
        return [json.loads(row['payload']) for row in rows]

    def remember_journey_constraints(self, user_id, session_id, intent, document):
        """Record a user requirement only when a real activity fulfills it."""
        from .journey_constraints import reconcile_requirements
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
                     json.dumps(requirement, ensure_ascii=False), now))

    def _pending_constraint_intents(self, user_id, session_id, draft_version):
        if not draft_version:
            return []
        with self._lock:
            row = self._connection.execute(
                'SELECT payload FROM working_constraint_intents '
                'WHERE user_id=? AND session_id=? AND draft_version=?',
                (user_id, session_id, draft_version)).fetchone()
        result = json.loads(row['payload']) if row else []
        return [intent for intent in result if isinstance(intent, dict)]

    def save_working_itinerary(self, user_id, session_id, document, *,
                               base_version, expected_draft_version=0, issues=None,
                               previous=None, intent=None):
        """Persist only structurally sound, editable drafts with optimistic locking.

        This path intentionally skips schedule/intent certification. It does not
        replace active_itineraries or count as a verified itinerary version.
        """
        from .trip_state import validate_itinerary
        from .working_drafts import review_draft
        prior_intents = self._pending_constraint_intents(user_id, session_id,
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
                                  _intent_issues(candidate, baseline, pending))
        issues = current_issues + (issues if isinstance(issues, list) else [])
        if not isinstance(issues, list):
            issues = []
        clean_issues = []
        seen_issues = set()
        for entry in issues[:100]:
            if isinstance(entry, dict):
                key = json.dumps(entry, sort_keys=True, default=str)
                if key in seen_issues:
                    continue
                seen_issues.add(key)
                clean_issues.append({key: entry[key] for key in
                                     ('code', 'day', 'activity_index', 'detail') if key in entry})
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connection:
            self._connection.execute('BEGIN IMMEDIATE')
            verified = self._connection.execute(
                'SELECT version FROM active_itineraries WHERE user_id=? AND session_id=?',
                (user_id, session_id)).fetchone()
            current_verified = verified['version'] if verified else 0
            if current_verified != base_version:
                raise ValueError('Verified trip changed while preparing a draft.')
            row = self._connection.execute(
                'SELECT draft_version FROM working_itineraries WHERE user_id=? AND session_id=?',
                (user_id, session_id)).fetchone()
            current_draft = row['draft_version'] if row else 0
            if current_draft != expected_draft_version:
                raise ValueError('Working draft changed during another edit.')
            last = self._connection.execute(
                'SELECT MAX(draft_version) FROM working_itinerary_versions WHERE user_id=? AND session_id=?',
                (user_id, session_id)).fetchone()[0] or 0
            new_version = max(current_draft, last) + 1
            self._connection.execute(
                'INSERT OR REPLACE INTO working_itineraries VALUES(?,?,?,?,?,?,?)',
                (user_id, session_id, new_version, base_version,
                 json.dumps(candidate, ensure_ascii=False),
                 json.dumps(clean_issues, ensure_ascii=False, default=str), now))
            self._connection.execute(
                'INSERT INTO working_itinerary_versions VALUES(?,?,?,?,?,?,?)',
                (user_id, session_id, new_version, base_version,
                 json.dumps(candidate, ensure_ascii=False),
                 json.dumps(clean_issues, ensure_ascii=False, default=str), now))
            self._connection.execute(
                'INSERT INTO working_constraint_intents VALUES(?,?,?,?)',
                (user_id, session_id, new_version,
                 json.dumps(pending_intents, ensure_ascii=False)))
        return {'draft_version': new_version, 'base_version': base_version,
                'itinerary': candidate, 'issues': clean_issues,
                'updated_at': now, 'status': 'draft'}

    def working_itinerary_versions(self, user_id, session_id):
        with self._lock:
            rows = self._connection.execute(
                'SELECT draft_version,base_version,updated_at FROM working_itinerary_versions '
                'WHERE user_id=? AND session_id=? ORDER BY draft_version DESC',
                (user_id, session_id)).fetchall()
        return [dict(row) for row in rows]

    def working_itinerary_history(self, user_id, session_id):
        """Compatibility with the prior public archive interface."""
        return self.working_itinerary_versions(user_id, session_id)

    def working_itinerary_version(self, user_id, session_id, version):
        with self._lock:
            row = self._connection.execute(
                'SELECT * FROM working_itinerary_versions WHERE user_id=? AND session_id=? AND draft_version=?',
                (user_id, session_id, version)).fetchone()
        if not row:
            return None
        return {'draft_version': row['draft_version'], 'base_version': row['base_version'],
                'itinerary': json.loads(row['document']), 'issues': json.loads(row['issues']),
                'updated_at': row['updated_at'], 'status': 'draft'}

    def save_visible_itinerary(self, user_id, session_id, *,
                               expected_draft_version=None,
                               expected_verified_version=None):
        """Save exactly the visible version as an immutable snapshot.

        This does not certify a provisional draft, create a new draft version,
        or overwrite an accepted itinerary. Concurrency guards and the insert
        run inside one SQLite write transaction.
        """
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connection:
            self._connection.execute('BEGIN IMMEDIATE')
            verified = self._connection.execute(
                'SELECT version,document FROM active_itineraries '
                'WHERE user_id=? AND session_id=?', (user_id, session_id)).fetchone()
            draft = self._connection.execute(
                'SELECT draft_version,base_version,document,issues FROM working_itineraries '
                'WHERE user_id=? AND session_id=?', (user_id, session_id)).fetchone()
            current_verified = verified['version'] if verified else 0
            if draft and draft['base_version'] != current_verified:
                draft = None  # Never save a draft based on an obsolete accepted trip.
            current_draft = draft['draft_version'] if draft else 0
            if (expected_verified_version is not None and
                    expected_verified_version != current_verified):
                raise ValueError('The verified version has changed. Refresh before saving.')
            if (expected_draft_version is not None and
                    expected_draft_version != current_draft):
                raise ValueError('The working draft has changed. Refresh before saving.')
            if draft:
                status, version, document, issues = (
                    'draft', current_draft, draft['document'], draft['issues'])
            elif verified:
                status, version, document, issues = (
                    'verified', current_verified, verified['document'], '[]')
            else:
                raise ValueError('No itinerary exists in this Journey to save.')
            cursor = self._connection.execute(
                'INSERT INTO saved_trip_snapshots('
                'user_id,session_id,source_status,source_version,document,issues,saved_at) '
                'VALUES(?,?,?,?,?,?,?)',
                (user_id, session_id, status, version, document, issues, now))
        return {'saved_id': cursor.lastrowid, 'source_status': status,
                'source_version': version, 'itinerary': json.loads(document),
                'issues': json.loads(issues), 'saved_at': now}

    def saved_trip_snapshots(self, user_id, session_id):
        with self._lock:
            rows = self._connection.execute(
                'SELECT id,source_status,source_version,saved_at FROM saved_trip_snapshots '
                'WHERE user_id=? AND session_id=? ORDER BY id DESC',
                (user_id, session_id)).fetchall()
        return [dict(row) for row in rows]

    def saved_trip_snapshot(self, user_id, session_id, saved_id):
        if type(saved_id) is not int or saved_id < 1:
            return None
        with self._lock:
            row = self._connection.execute(
                'SELECT id,source_status,source_version,document,issues,saved_at '
                'FROM saved_trip_snapshots WHERE user_id=? AND session_id=? AND id=?',
                (user_id, session_id, saved_id)).fetchone()
        if row is None:
            return None
        return {'saved_id': row['id'], 'source_status': row['source_status'],
                'source_version': row['source_version'],
                'itinerary': json.loads(row['document']),
                'issues': json.loads(row['issues']), 'saved_at': row['saved_at']}

    def save_current_itinerary(self, user_id, session_id, *, expected_version, expected_draft_version):
        """A save command never asks a model to invent an additional edit."""
        from .working_drafts import review_draft
        active = self.active_itinerary(user_id, session_id)
        draft = self.working_itinerary(user_id, session_id)
        if (active['version'] if active else 0) != expected_version:
            raise ValueError('The active itinerary was updated by another request.')
        if (draft['draft_version'] if draft else 0) != expected_draft_version:
            raise ValueError('Working draft changed during another edit.')
        if not draft:
            if not active:
                raise ValueError('There is no itinerary to save.')
            return {'active_itinerary': active, 'working_draft': None, 'saved_status': 'validated'}
        pending_intents = self._pending_constraint_intents(
            user_id, session_id, draft['draft_version'])
        candidate, issues = review_draft(
            draft['itinerary'],
            protected_constraints=self.journey_constraints(user_id, session_id),
            constraint_intents=pending_intents)
        from .validation_gate import _intent_issues
        for pending in pending_intents:
            issues.extend(issue.as_dict() for issue in
                          _intent_issues(candidate, active['itinerary'] if active else None,
                                         pending))
        # Saving is not a new edit and cannot resolve an earlier unmet request.
        issues.extend(draft.get('issues') or [])
        if issues:
            saved = self.save_working_itinerary(user_id, session_id, candidate,
                base_version=expected_version, expected_draft_version=expected_draft_version, issues=issues)
            return {'active_itinerary': active, 'working_draft': saved, 'saved_status': 'draft'}
        active = self.save_itinerary(user_id, session_id, candidate,
            expected_version=expected_version, expected_draft_version=expected_draft_version,
            operation='revise' if active else 'create',
            constraint_intents=pending_intents)
        return {'active_itinerary': active, 'working_draft': None, 'saved_status': 'validated'}

    def clear_working_itinerary(self, user_id, session_id):
        with self._lock, self._connection:
            self._connection.execute('DELETE FROM working_itineraries '
                                     'WHERE user_id=? AND session_id=?', (user_id, session_id))

    def save_itinerary(self, user_id, session_id, document, *, expected_version=0,
                       operation=None, intent=None, trusted_sources=None, expected_draft_version=None,
                       constraint_intents=None):
        """The last, atomic pre-commit guard for every itinerary write path.

        Recheck the exact current row inside the same SQLite transaction, so
        neither a legacy router nor a future API caller can evade validation.
        """
        from .trip_state import validate_itinerary
        from .validation_gate import validate_for_commit
        if not isinstance(document, dict):
            raise ValueError('Itinerary must be an object.')
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connection:
            self._connection.execute('BEGIN IMMEDIATE')
            row = self._connection.execute(
                'SELECT version,document FROM active_itineraries WHERE user_id=? AND session_id=?',
                (user_id, session_id)).fetchone()
            current = row['version'] if row else 0
            if current != expected_version:
                raise ValueError('The active itinerary was updated by another request.')
            prior = json.loads(row['document']) if row else None
            working = self._connection.execute(
                'SELECT draft_version,base_version,document FROM working_itineraries WHERE user_id=? AND session_id=?',
                (user_id, session_id)).fetchone()
            if expected_draft_version is not None and (working['draft_version'] if working else 0) != expected_draft_version:
                raise ValueError('Working draft changed during another edit.')
            intent_previous = (json.loads(working['document'])
                               if working and expected_draft_version and working['base_version'] == current else prior)
            # This API accepts a COMPLETE candidate snapshot. Never treat missing
            # summary/derived fields as an instruction to inherit old copy.
            # `revise` still restricts destination and day count, but does not
            # perform the patch merge that belongs in validate_itinerary(changes).
            effective = ('create' if prior is None else
                         (operation or 'revise'))
            if effective not in {'create', 'revise'}:
                raise ValueError('Unsupported itinerary operation.')
            document = validate_itinerary(
                document, previous=prior,
                operation='snapshot' if effective == 'revise' else 'create')
            from .journey_constraints import reconcile_requirements
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
                document, previous=prior)
            serialized = json.dumps(document, ensure_ascii=False)
            new_version = current + 1
            self._connection.execute(
                'INSERT OR REPLACE INTO active_itineraries VALUES(?,?,?,?,?)',
                (user_id, session_id, new_version, serialized, now))
            self._connection.execute(
                'INSERT INTO itinerary_versions VALUES(?,?,?,?,?)',
                (user_id, session_id, new_version, serialized, now))
            self._connection.execute(
                'DELETE FROM working_itineraries WHERE user_id=? AND session_id=?',
                (user_id, session_id))
            self._connection.execute(
                'DELETE FROM journey_constraints WHERE user_id=? AND session_id=?',
                (user_id, session_id))
            for requirement in next_constraints:
                self._connection.execute(
                    'INSERT INTO journey_constraints VALUES(?,?,?,?,?)',
                    (user_id, session_id, requirement['key'],
                     json.dumps(requirement, ensure_ascii=False), now))
        return {'version': new_version, 'itinerary': document, 'updated_at': now}

    def restore_itinerary_version(self, user_id, session_id, version, *, expected_version):
        """Restore an accepted snapshot as a NEW version, never bypassing constraints."""
        snapshot = self.itinerary_version_document(user_id, session_id, version)
        if snapshot is None:
            raise ValueError('Requested itinerary version does not exist in this session.')
        return self.save_itinerary(user_id, session_id, snapshot,
                                   expected_version=expected_version, operation='revise')

    def record_revision_diagnostic(self, user_id, session_id, *, baseline_version,
                                   stage, details):
        """Bounded per-session diagnostic audit, not raw prompt/model telemetry."""
        if not isinstance(details, dict):
            details = {'error': str(details)[:500]}
        serialized = json.dumps(details, ensure_ascii=False, default=str)
        if len(serialized) > 16000:
            serialized = json.dumps({
                'truncated': True,
                'error': str(details.get('message') or details.get('error') or '')[:1000],
                'stage': str(stage)[:70],
                'note': 'Diagnostic exceeded the bounded storage limit.'},
                ensure_ascii=False)
        with self._lock, self._connection:
            self._connection.execute(
                'INSERT INTO revision_diagnostics(user_id,session_id,baseline_version,stage,details,created_at) '
                'VALUES(?,?,?,?,?,?)',
                (user_id, session_id, int(baseline_version), str(stage)[:70],
                 serialized, datetime.now(timezone.utc).isoformat()))
            self._connection.execute(
                'DELETE FROM revision_diagnostics WHERE user_id=? AND session_id=? AND id NOT IN '
                '(SELECT id FROM revision_diagnostics WHERE user_id=? AND session_id=? '
                'ORDER BY id DESC LIMIT 30)',
                (user_id, session_id, user_id, session_id))

    def revision_diagnostics(self, user_id, session_id, limit=30):
        with self._lock:
            rows = self._connection.execute(
                'SELECT baseline_version,stage,details,created_at FROM revision_diagnostics '
                'WHERE user_id=? AND session_id=? ORDER BY id DESC LIMIT ?',
                (user_id, session_id, max(1, min(int(limit), 30)))).fetchall()
        return [{'baseline_version': r['baseline_version'], 'stage': r['stage'],
                 'details': json.loads(r['details']), 'created_at': r['created_at']}
                for r in rows]

    def add_message(self, user_id, session_id, role, content):
        with self._lock, self._connection:
            self._connection.execute('INSERT INTO messages(user_id,session_id,role,content,created_at) VALUES(?,?,?,?,?)', (user_id, session_id, role, content, datetime.now(timezone.utc).isoformat()))

    def messages(self, user_id, session_id, limit=40):
        # The UI needs the complete session after refresh, while the model
        # context continues to use the caller-selected, bounded history.
        with self._lock:
            query = ('SELECT role, content, created_at FROM messages '
                     'WHERE user_id=? AND session_id=? ORDER BY id DESC')
            parameters = (user_id, session_id)
            if limit is not None:
                query += ' LIMIT ?'
                parameters += (max(0, int(limit)),)
            rows = self._connection.execute(query, parameters).fetchall()
        return [dict(row) for row in reversed(rows)]

    def sessions(self, user_id):
        # A journey's preview is the FIRST user message, not the smallest
        # alphabetically or the latest message sent to that conversation.
        with self._lock:
            rows = self._connection.execute('''
                SELECT m.session_id,
                       (SELECT first.content FROM messages AS first
                        WHERE first.user_id = m.user_id
                          AND first.session_id = m.session_id
                          AND first.role = 'user'
                        ORDER BY first.id ASC LIMIT 1) AS preview,
                       MAX(m.id) AS last_id
                FROM messages AS m
                WHERE m.user_id = ?
                GROUP BY m.session_id
                ORDER BY last_id DESC
                LIMIT 100
            ''', (user_id,)).fetchall()
        return [{'session_id': row['session_id'],
                 'preview': (row['preview'] or 'New trip')[:65]} for row in rows]

    def search_history(self,user_id,query,limit=8):
        terms=[w.lower() for w in query.split() if len(w)>2][:10]
        if not terms: return []
        with self._lock:
            rows=self._connection.execute('SELECT session_id,role,content FROM messages WHERE user_id=? ORDER BY id DESC LIMIT 400',(user_id,)).fetchall()
        ranked=[(sum(1 for w in terms if w in row['content'].lower()),dict(row)) for row in rows]
        return [row for score,row in sorted(ranked,key=lambda x:x[0],reverse=True) if score>0][:limit]

    def update_preference(self,user_id,category,value,scope='global',session_id='default',consent=False):
        if not consent: raise PermissionError('Explicit consent is required to persist a preference.')
        selected_session=session_id if scope=='trip' else '*'
        with self._lock,self._connection:
            self._connection.execute('INSERT OR REPLACE INTO preferences VALUES(?,?,?,?,?,?)',(user_id,scope,selected_session,category,value,datetime.now(timezone.utc).isoformat()))
        return self.preferences(user_id,session_id)

    def preferences(self,user_id,session_id='default'):
        with self._lock:
            rows=self._connection.execute("SELECT category,value,scope FROM preferences WHERE user_id=? AND (scope='global' OR (scope='trip' AND session_id=?)) ORDER BY scope ASC",(user_id,session_id)).fetchall()
        return {row['category']:{'value':row['value'],'scope':row['scope']} for row in rows}

    def delete_preference(self,user_id,category,scope='global',session_id='default'):
        with self._lock,self._connection:
            self._connection.execute('DELETE FROM preferences WHERE user_id=? AND category=? AND scope=? AND session_id=?',(user_id,category,scope,session_id if scope=='trip' else '*'))

    def add_document(self,user_id,title,content):
        with self._lock,self._connection:
            cursor=self._connection.execute('INSERT INTO documents(user_id,title,content,created_at) VALUES(?,?,?,?)',(user_id,title,content,datetime.now(timezone.utc).isoformat()))
            return cursor.lastrowid

    def get_document(self,user_id,document_id):
        with self._lock:
            row=self._connection.execute('SELECT id,title,content,created_at FROM documents WHERE user_id=? AND id=?', (user_id,document_id)).fetchone()
        return dict(row) if row else None

    def _purge_document_index(self,user_id,document_id):
        # Called within the caller's transaction to avoid stale indexed content.
        for table in ('rag_vectors','rag_chunks'):
            if self._connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                self._connection.execute(f'DELETE FROM {table} WHERE user_id=? AND document_id=?', (user_id,document_id))

    def update_document(self,user_id,document_id,title,content):
        with self._lock,self._connection:
            cursor=self._connection.execute('UPDATE documents SET title=?,content=? WHERE user_id=? AND id=?', (title,content,user_id,document_id))
            if cursor.rowcount:self._purge_document_index(user_id,document_id)
        return cursor.rowcount>0

    def delete_document(self,user_id,document_id):
        with self._lock,self._connection:
            cursor=self._connection.execute('DELETE FROM documents WHERE user_id=? AND id=?', (user_id,document_id))
            if cursor.rowcount:self._purge_document_index(user_id,document_id)
        return cursor.rowcount>0

    def document_ids(self,user_id):
        with self._lock:
            return [row['id'] for row in self._connection.execute('SELECT id FROM documents WHERE user_id=?', (user_id,)).fetchall()]

    def documents(self,user_id):
        with self._lock:
            rows=self._connection.execute('SELECT id,title,content,created_at FROM documents WHERE user_id=? ORDER BY id DESC',(user_id,)).fetchall()
        return [dict(row) for row in rows]

    def delete_user(self,user_id):
        with self._lock,self._connection:
            for table in ('rag_vectors','rag_chunks','messages','preferences','documents','expenses','trips','active_itineraries','itinerary_versions','revision_diagnostics','working_itineraries','working_itinerary_versions','saved_trip_snapshots','journey_constraints','working_constraint_intents'):
                if table.startswith('rag_') and not self._connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone():
                    continue
                self._connection.execute(f'DELETE FROM {table} WHERE user_id=?',(user_id,))

    def add_expense(self,user_id,session_id,amount,currency,category):
        with self._lock,self._connection:
            self._connection.execute('INSERT INTO expenses(user_id,session_id,amount,currency,category,created_at) VALUES(?,?,?,?,?,?)',(user_id,session_id,amount,currency,category,datetime.now(timezone.utc).isoformat()))

    def expenses(self,user_id):
        with self._lock:
            return [dict(row) for row in self._connection.execute('SELECT id,amount,currency,category,created_at FROM expenses WHERE user_id=? ORDER BY id DESC LIMIT 200',(user_id,)).fetchall()]

    def add_trip(self,user_id,title,summary):
        with self._lock,self._connection:
            self._connection.execute('INSERT INTO trips(user_id,title,summary,created_at) VALUES(?,?,?,?)',(user_id,title,summary,datetime.now(timezone.utc).isoformat()))

    def trips(self,user_id):
        with self._lock:
            return [dict(row) for row in self._connection.execute('SELECT id,title,summary,created_at FROM trips WHERE user_id=? ORDER BY id DESC LIMIT 100',(user_id,)).fetchall()]
