"""Persistent, user-scoped SQLite retrieval index.

Vectors are stored separately from text chunks and keyed by the *exact* model
identifier. SQLite is the development backend; Retrieval accepts another index
backend implementing the same methods. No external ANN service is required.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
import re
import sqlite3


def tokenize(text: str) -> list[str]:
    """Portable lexical tokenization, including CJK single-character terms."""
    return re.findall(r"[^\W_]+", text.casefold(), flags=re.UNICODE)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SQLiteRetrievalIndex:
    """A transactional index sharing the MemoryStore SQLite connection/lock."""

    def __init__(self, store):
        self.store = store
        with store._lock, store._connection:
            store._connection.executescript('''
                CREATE TABLE IF NOT EXISTS rag_chunks (
                    user_id TEXT NOT NULL,
                    document_id INTEGER NOT NULL,
                    chunk_number INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    text TEXT NOT NULL,
                    offset INTEGER NOT NULL,
                    text_hash TEXT NOT NULL,
                    terms_json TEXT NOT NULL,
                    PRIMARY KEY (user_id, document_id, chunk_number)
                );
                CREATE INDEX IF NOT EXISTS ix_rag_chunks_user ON rag_chunks(user_id,document_id);
                CREATE TABLE IF NOT EXISTS rag_vectors (
                    user_id TEXT NOT NULL,
                    document_id INTEGER NOT NULL,
                    chunk_number INTEGER NOT NULL,
                    model_id TEXT NOT NULL,
                    text_hash TEXT NOT NULL,
                    dimensions INTEGER NOT NULL,
                    vector_json TEXT NOT NULL,
                    PRIMARY KEY (user_id,document_id,chunk_number,model_id),
                    FOREIGN KEY (user_id,document_id,chunk_number)
                    REFERENCES rag_chunks (user_id,document_id,chunk_number)
                );
                CREATE INDEX IF NOT EXISTS ix_rag_vectors_scope ON rag_vectors(user_id,model_id);
            ''')

    @staticmethod
    def _hash(text):
        return hashlib.sha256(text.encode('utf-8')).hexdigest()

    def replace_document(self, user_id, doc_id, chunks, model_id=None, vectors=None):
        """Atomically replace chunks/vectors; invalidates all old model vectors."""
        if vectors is not None and (not model_id or len(chunks) != len(vectors)):
            raise ValueError('One model identifier and one vector per chunk are required.')
        prepared = []
        for n, chunk in enumerate(chunks):
            t = str(chunk['text'])
            terms = Counter(tokenize(t))
            prepared.append((user_id, doc_id, n, str(chunk['title']), t,
                             int(chunk['offset']), self._hash(t), json.dumps(terms, ensure_ascii=False)))
        encoded = []
        if vectors is not None:
            for n, vector in enumerate(vectors):
                normalized = [float(v) for v in vector]
                if not normalized or not all(math.isfinite(v) for v in normalized):
                    raise ValueError('Nonempty finite embedding vector required.')
                encoded.append((user_id, doc_id, n, model_id, prepared[n][6],
                                len(normalized), json.dumps(normalized)))
        with self.store._lock, self.store._connection:
            self.store._connection.execute('DELETE FROM rag_vectors WHERE user_id=? AND document_id=?', (user_id,doc_id))
            self.store._connection.execute('DELETE FROM rag_chunks WHERE user_id=? AND document_id=?', (user_id,doc_id))
            self.store._connection.executemany(
                'INSERT INTO rag_chunks VALUES (?,?,?,?,?,?,?,?)', prepared)
            self.store._connection.executemany(
                'INSERT INTO rag_vectors VALUES (?,?,?,?,?,?,?)', encoded)
        return {'chunks':len(prepared),'embedded':len(encoded),'model':model_id if encoded else None}

    def delete_document(self, user_id, doc_id):
        with self.store._lock, self.store._connection:
            self.store._connection.execute('DELETE FROM rag_vectors WHERE user_id=? AND document_id=?',(user_id,doc_id))
            self.store._connection.execute('DELETE FROM rag_chunks WHERE user_id=? AND document_id=?',(user_id,doc_id))

    def delete_user(self, user_id):
        with self.store._lock, self.store._connection:
            for table in ('rag_vectors','rag_chunks'):
                self.store._connection.execute(f'DELETE FROM {table} WHERE user_id=?',(user_id,))

    def indexed_document_ids(self, user_id):
        with self.store._lock:
            rows = self.store._connection.execute(
                'SELECT DISTINCT document_id FROM rag_chunks WHERE user_id=?', (user_id,)).fetchall()
        return {row['document_id'] for row in rows}

    def records(self, user_id, model_id=None):
        """Read indexed chunks, optionally attaching matching model embeddings."""
        with self.store._lock:
            rows = self.store._connection.execute('''
                SELECT c.user_id,c.document_id,c.chunk_number,c.title,c.text,c.offset,
                       c.text_hash,c.terms_json,v.vector_json,v.dimensions
                FROM rag_chunks c
                LEFT JOIN rag_vectors v ON c.user_id=v.user_id AND c.document_id=v.document_id
                   AND c.chunk_number=v.chunk_number AND v.model_id=? AND v.text_hash=c.text_hash
                WHERE c.user_id=? ORDER BY c.document_id,c.chunk_number
            ''', (model_id or '', user_id)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item['terms'] = json.loads(item.pop('terms_json'))
            encoded = item.pop('vector_json')
            item['vector'] = json.loads(encoded) if encoded is not None else None
            result.append(item)
        return result

    def stats(self, user_id, model_id):
        with self.store._lock:
            doc_count = self.store._connection.execute(
                'SELECT COUNT(*) n FROM documents WHERE user_id=?',(user_id,)).fetchone()['n']
            chunk_count = self.store._connection.execute(
                'SELECT COUNT(*) n FROM rag_chunks WHERE user_id=?',(user_id,)).fetchone()['n']
            vector_count = self.store._connection.execute('''
                SELECT COUNT(*) n FROM rag_vectors v JOIN rag_chunks c
                  ON v.user_id=c.user_id AND v.document_id=c.document_id
                  AND v.chunk_number=c.chunk_number AND v.text_hash=c.text_hash
                WHERE v.user_id=? AND v.model_id=?
            ''',(user_id,model_id)).fetchone()['n']
        return {'documents':doc_count,'chunks':chunk_count,'embedded_chunks':vector_count,
                'model':model_id,'stale_chunks':chunk_count-vector_count}
