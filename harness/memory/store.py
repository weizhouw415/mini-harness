from __future__ import annotations

import re
from hashlib import sha256
from uuid import uuid4

from harness.memory.redaction import _safe_text
from harness.storage.sqlite import Conflict, NotFound, now


_CREDENTIAL = re.compile(
    r"(?i)(?:password|passwd|secret|api[ _.-]?key|access[ _.-]?token|"
    r"refresh[ _.-]?token|authorization|private[ _.-]?key|"
    r"密码|口令|密钥|令牌|凭证|"
    r"\bsk-[A-Za-z0-9_-]{12,}|\b(?:ghp|gho|github_pat)_[A-Za-z0-9_]{12,}|"
    r"\bAKIA[A-Z0-9]{16}\b|-----BEGIN [A-Z ]*PRIVATE KEY-----)"
)


def _contains_credential(key: str, content: str) -> bool:
    return bool(_CREDENTIAL.search(key) or _CREDENTIAL.search(content) or
                _safe_text(key, len(key)) != key or _safe_text(content, len(content)) != content)


class MemoryStore:
    """Stores memory in the control-plane database shared by every worker."""

    def __init__(self, runs):
        self.runs = runs
        with runs.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS memories (
                    id TEXT PRIMARY KEY,
                    scope TEXT NOT NULL CHECK(scope IN ('session', 'global')),
                    scope_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    memory_key TEXT NOT NULL,
                    content TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(scope, scope_id, kind, memory_key)
                );
                CREATE INDEX IF NOT EXISTS memories_scope ON memories(scope, scope_id, updated_at);
                CREATE TABLE IF NOT EXISTS memory_versions (
                    memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
                    version INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(memory_id, version)
                );
                CREATE TABLE IF NOT EXISTS memory_evidence (
                    memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
                    run_id TEXT NOT NULL REFERENCES runs(id),
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(memory_id, run_id)
                );
                CREATE TABLE IF NOT EXISTS memory_events (
                    seq INTEGER PRIMARY KEY,
                    memory_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    version INTEGER,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS memory_tombstones (
                    scope TEXT NOT NULL,
                    scope_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    key_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(scope, scope_id, kind, key_hash)
                );
            """)
            # Trigram indexes Chinese text without depending on spaces between words.
            try:
                db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(memory_id UNINDEXED, memory_key, content, tokenize='trigram')")
                self.fts = True
            except Exception as error:
                import sqlite3
                if not isinstance(error, sqlite3.OperationalError):
                    raise
                self.fts = False

    @staticmethod
    def _scope_id(session_id, scope):
        if scope == "global":
            return "installation"
        if scope == "session" and session_id:
            return session_id
        raise ValueError("A valid session and memory scope are required.")

    @staticmethod
    def _visible(row, session_id, include_global):
        return (row["scope"] == "session" and row["scope_id"] == session_id or
                row["scope"] == "global" and include_global)

    def remember(self, session_id, key, content, *, scope="session", kind="preference",
                 source_run_id=None, expected_version=None, explicit=True):
        scope_id = self._scope_id(session_id, scope)
        if not isinstance(key, str) or not 1 <= len(key.strip()) <= 200:
            raise ValueError("Memory key must contain 1 to 200 characters.")
        if not isinstance(content, str) or not 1 <= len(content.strip()) <= 4000:
            raise ValueError("Memory content must contain 1 to 4000 characters.")
        if kind not in {"preference", "fact", "project", "episode", "short_term"}:
            raise ValueError("Unsupported memory type.")
        key, content = key.strip().casefold(), content.strip()
        if _contains_credential(key, content):
            raise ValueError("Credentials cannot be saved as memory.")
        digest = sha256(key.encode()).hexdigest()
        timestamp = now()
        with self.runs.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM sessions WHERE id = ?", (session_id,)).fetchone():
                raise NotFound("Session not found.")
            if source_run_id and not db.execute("SELECT 1 FROM runs WHERE id = ? AND session_id = ?", (source_run_id, session_id)).fetchone():
                raise NotFound("Memory source run not found.")
            tombstone = db.execute("SELECT 1 FROM memory_tombstones WHERE scope=? AND scope_id=? AND kind=? AND key_hash=?",
                                   (scope, scope_id, kind, digest)).fetchone()
            if tombstone and not explicit:
                raise Conflict("This memory was forgotten and requires an explicit request to restore.")
            if explicit:
                db.execute("DELETE FROM memory_tombstones WHERE scope=? AND scope_id=? AND kind=? AND key_hash=?",
                           (scope, scope_id, kind, digest))
            existing = db.execute("SELECT * FROM memories WHERE scope=? AND scope_id=? AND kind=? AND memory_key=?",
                                  (scope, scope_id, kind, key)).fetchone()
            if expected_version is not None and (existing is None or existing["version"] != expected_version):
                raise Conflict("Memory version has changed.")
            if existing:
                memory_id = existing["id"]
                version = existing["version"] + (existing["content"] != content)
                if existing["content"] != content:
                    db.execute("UPDATE memories SET content=?, version=?, updated_at=? WHERE id=?",
                               (content, version, timestamp, memory_id))
                    db.execute("INSERT INTO memory_versions VALUES (?, ?, ?, ?)", (memory_id, version, content, timestamp))
                    db.execute("INSERT INTO memory_events(memory_id, action, version, created_at) VALUES (?, 'update', ?, ?)",
                               (memory_id, version, timestamp))
            else:
                memory_id, version = uuid4().hex, 1
                db.execute("INSERT INTO memories VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                           (memory_id, scope, scope_id, kind, key, content, version, timestamp, timestamp))
                db.execute("INSERT INTO memory_versions VALUES (?, ?, ?, ?)", (memory_id, version, content, timestamp))
                db.execute("INSERT INTO memory_events(memory_id, action, version, created_at) VALUES (?, 'create', ?, ?)",
                           (memory_id, version, timestamp))
            if source_run_id:
                db.execute("INSERT OR IGNORE INTO memory_evidence VALUES (?, ?, ?)", (memory_id, source_run_id, timestamp))
            if self.fts:
                db.execute("DELETE FROM memory_fts WHERE memory_id=?", (memory_id,))
                db.execute("INSERT INTO memory_fts(memory_id, memory_key, content) VALUES (?, ?, ?)", (memory_id, key, content))
            return dict(db.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone())

    def get(self, session_id, memory_id, *, include_global=False):
        with self.runs.connect() as db:
            row = db.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone()
            if row is None or not self._visible(row, session_id, include_global):
                raise NotFound("Memory not found.")
            return dict(row)

    def source(self, session_id, memory_id, *, include_global=False):
        self.get(session_id, memory_id, include_global=include_global)
        with self.runs.connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT run_id, created_at FROM memory_evidence WHERE memory_id=? ORDER BY created_at", (memory_id,))]

    def forget(self, session_id, memory_id, *, include_global=False):
        with self.runs.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone()
            if row is None or not self._visible(row, session_id, include_global):
                raise NotFound("Memory not found.")
            digest = sha256(row["memory_key"].encode()).hexdigest()
            db.execute("INSERT OR REPLACE INTO memory_tombstones VALUES (?, ?, ?, ?, ?)",
                       (row["scope"], row["scope_id"], row["kind"], digest, now()))
            if self.fts:
                db.execute("DELETE FROM memory_fts WHERE memory_id=?", (memory_id,))
            db.execute("DELETE FROM memories WHERE id=?", (memory_id,))
            db.execute("INSERT INTO memory_events(memory_id, action, version, created_at) VALUES (?, 'forget', ?, ?)",
                       (memory_id, row["version"], now()))
        return {"id": memory_id, "forgotten": True}

    def search(self, session_id, query="", *, limit=5, include_global=False):
        if not isinstance(query, str):
            raise ValueError("Query must be text.")
        if not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("Limit must be between 1 and 100.")
        # Scope first, then rank matches. LIKE also supports short Chinese words.
        terms = list(dict.fromkeys(re.findall(r"[\u4e00-\u9fff]{2,}|[\w.-]+", query.casefold())))
        with self.runs.connect() as db:
            scope_filter = "((scope='session' AND scope_id=?) OR (scope='global' AND ?))"
            parameters = [session_id, bool(include_global)]
            clauses = []
            if terms and self.fts:
                fragments = list(dict.fromkeys(
                    term[index:index + 3]
                    for term in terms for index in range(max(0, len(term) - 2))
                ))[:64]
                if fragments:
                    expression = " OR ".join('"' + fragment.replace('"', '""') + '"' for fragment in fragments)
                    clauses.append("id IN (SELECT memory_id FROM memory_fts WHERE memory_fts MATCH ?)")
                    parameters.append(expression)
            for term in terms[:32]:
                escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                clauses.append("(memory_key LIKE ? ESCAPE '\\' OR content LIKE ? ESCAPE '\\')")
                parameters.extend([f"%{escaped}%", f"%{escaped}%"])
            if terms:
                scope_filter += " AND (" + " OR ".join(clauses) + ")"
            rows = db.execute("SELECT * FROM memories WHERE " + scope_filter, parameters).fetchall()
        ranked = []
        for row in rows:
            haystack = (row["memory_key"] + " " + row["content"]).casefold()
            score = 0
            for term in terms:
                if term in haystack:
                    score += 3
                elif len(term) >= 3:
                    score += sum(1 for i in range(len(term) - 2) if term[i:i+3] in haystack)
            if not query.strip() or score:
                ranked.append((score, row["updated_at"], dict(row)))
        ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return [item[2] for item in ranked[:limit]]

    def apply_extracted(self, db, session_id, source_run_id, run_created_at, candidates, *, include_global=False):
        """Apply candidates while holding the job completion transaction."""
        if not db.execute("SELECT 1 FROM runs WHERE id=? AND session_id=?", (source_run_id, session_id)).fetchone():
            raise NotFound("Memory source run not found.")
        applied = []
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            key, content = candidate.get("key"), candidate.get("content")
            kind, scope = candidate.get("kind", "preference"), candidate.get("scope", "session")
            if (not isinstance(key, str) or not 1 <= len(key.strip()) <= 200 or
                not isinstance(content, str) or not 1 <= len(content.strip()) <= 4000 or
                kind not in {"preference", "fact", "project", "episode", "short_term"} or
                scope not in {"session", "global"} or (scope == "global" and not include_global)):
                continue
            key, content = key.strip().casefold(), content.strip()
            if _contains_credential(key, content):
                continue
            scope_id = self._scope_id(session_id, scope)
            digest = sha256(key.encode()).hexdigest()
            if db.execute("SELECT 1 FROM memory_tombstones WHERE scope=? AND scope_id=? AND kind=? AND key_hash=?",
                          (scope, scope_id, kind, digest)).fetchone():
                continue
            row = db.execute("SELECT * FROM memories WHERE scope=? AND scope_id=? AND kind=? AND memory_key=?",
                             (scope, scope_id, kind, key)).fetchone()
            if row and row["updated_at"] > run_created_at and row["content"] != content:
                continue
            timestamp = now()
            if row:
                memory_id = row["id"]
                version = row["version"] + (row["content"] != content)
                if row["content"] != content:
                    db.execute("UPDATE memories SET content=?, version=?, updated_at=? WHERE id=?",
                               (content, version, timestamp, memory_id))
                    db.execute("INSERT INTO memory_versions VALUES (?, ?, ?, ?)", (memory_id, version, content, timestamp))
                    db.execute("INSERT INTO memory_events(memory_id, action, version, created_at) VALUES (?, 'update', ?, ?)",
                               (memory_id, version, timestamp))
            else:
                memory_id, version = uuid4().hex, 1
                db.execute("INSERT INTO memories VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                           (memory_id, scope, scope_id, kind, key, content, version, timestamp, timestamp))
                db.execute("INSERT INTO memory_versions VALUES (?, ?, ?, ?)", (memory_id, version, content, timestamp))
                db.execute("INSERT INTO memory_events(memory_id, action, version, created_at) VALUES (?, 'create', ?, ?)",
                           (memory_id, version, timestamp))
            db.execute("INSERT OR IGNORE INTO memory_evidence VALUES (?, ?, ?)", (memory_id, source_run_id, timestamp))
            if self.fts:
                db.execute("DELETE FROM memory_fts WHERE memory_id=?", (memory_id,))
                db.execute("INSERT INTO memory_fts(memory_id, memory_key, content) VALUES (?, ?, ?)", (memory_id, key, content))
            applied.append(memory_id)
        return applied
