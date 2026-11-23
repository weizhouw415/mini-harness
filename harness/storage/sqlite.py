from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4


ApprovalMode = Literal["legacy", "manual", "auto", "allow"]


def now() -> str:
    return datetime.now(UTC).isoformat()


class NotFound(ValueError):
    pass


class Conflict(ValueError):
    pass


class RunStore:
    def __init__(self, directory: Path):
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = directory / "runs.sqlite3"
        with self.connect() as db:
            db.execute("PRAGMA journal_mode = WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL REFERENCES sessions(id),
                    requested_worker TEXT,
                    worker_id TEXT,
                    workspace_id TEXT NOT NULL,
                    task TEXT NOT NULL,
                    status TEXT NOT NULL,
                    output TEXT,
                    error TEXT,
                    approval TEXT,
                    approval_mode TEXT NOT NULL DEFAULT 'legacy'
                        CHECK(approval_mode IN ('legacy', 'manual', 'auto', 'allow')),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(id),
                    type TEXT NOT NULL,
                    data TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS run_events ON events(run_id, seq);
                CREATE INDEX IF NOT EXISTS session_runs ON runs(session_id, created_at);
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_session_run ON runs(session_id)
                    WHERE status IN ('queued', 'running', 'paused', 'awaiting_approval');
            """)
            db.execute("BEGIN IMMEDIATE")
            if "approval_mode" not in {row["name"] for row in db.execute("PRAGMA table_info(runs)")}:
                db.execute("""
                    ALTER TABLE runs ADD COLUMN approval_mode TEXT NOT NULL DEFAULT 'legacy'
                        CHECK(approval_mode IN ('legacy', 'manual', 'auto', 'allow'))
                """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.db, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def create(self, task: str, session_id: str | None, worker_id: str | None,
               approval_mode: ApprovalMode = "legacy") -> dict:
        run_id, workspace_id = uuid4().hex, uuid4().hex
        created = now()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if session_id is None:
                session_id = uuid4().hex
                db.execute("INSERT INTO sessions VALUES (?, ?)", (session_id, created))
            elif not db.execute("SELECT 1 FROM sessions WHERE id = ?", (session_id,)).fetchone():
                raise NotFound("Session not found.")
            try:
                db.execute(
                    """INSERT INTO runs (id, session_id, requested_worker, workspace_id, task,
                       status, approval_mode, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'queued', ?, ?, ?)""",
                    (run_id, session_id, worker_id, workspace_id, task, approval_mode, created, created),
                )
            except sqlite3.IntegrityError as error:
                raise Conflict("This session already has an unfinished run.") from error
        return self.get(run_id)

    def get(self, run_id: str) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise NotFound("Run not found.")
        run = dict(row)
        run["approval"] = json.loads(run["approval"]) if run["approval"] else None
        return run

    def previous(self, session_id: str, run_id: str) -> list[dict]:
        with self.connect() as db:
            rows = db.execute(
                """SELECT id, task, output, status, error FROM runs
                   WHERE session_id = ? AND id != ? AND status IN ('completed', 'failed', 'cancelled')
                   ORDER BY created_at, rowid""",
                (session_id, run_id),
            ).fetchall()
            prior = []
            for row in rows:
                item = dict(row)
                events = db.execute(
                    "SELECT seq, type, data, created_at FROM events WHERE run_id = ? ORDER BY seq",
                    (item["id"],),
                ).fetchall()
                item["events"] = [{**event, "data": json.loads(event["data"])} for event in events]
                prior.append(item)
        return prior

    def recent(self) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("""
                SELECT id, session_id, substr(task, 1, 160) AS task, status, updated_at
                FROM runs ORDER BY updated_at DESC, id DESC LIMIT 100
            """).fetchall()
        return [dict(row) for row in rows]

    def recent_sessions(self) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("""
                SELECT r.session_id AS id, r.id AS latest_run_id, r.status,
                       (SELECT substr(first.task, 1, 160) FROM runs first
                        WHERE first.session_id = r.session_id
                        ORDER BY first.created_at, first.id LIMIT 1) AS title
                FROM sessions s
                JOIN runs r ON r.id = (
                    SELECT latest.id FROM runs latest WHERE latest.session_id = s.id
                    ORDER BY latest.created_at DESC, latest.id DESC LIMIT 1
                )
                ORDER BY r.updated_at DESC, r.id DESC LIMIT 100
            """).fetchall()
        return [dict(row) for row in rows]

    def session_runs(self, session_id: str) -> list[dict]:
        with self.connect() as db:
            if not db.execute("SELECT 1 FROM sessions WHERE id = ?", (session_id,)).fetchone():
                raise NotFound("Session not found.")
            rows = db.execute("""
                SELECT id, session_id, task, status, output, error, approval, approval_mode
                FROM runs WHERE session_id = ? ORDER BY created_at, id
            """, (session_id,)).fetchall()
        return [{**row, "approval": json.loads(row["approval"]) if row["approval"] else None} for row in rows]

    def events(self, run_id: str) -> list[dict]:
        self.get(run_id)
        with self.connect() as db:
            rows = db.execute(
                "SELECT seq, type, data, created_at FROM events WHERE run_id = ? ORDER BY seq", (run_id,)
            ).fetchall()
        return [{**row, "data": json.loads(row["data"])} for row in rows]

    @staticmethod
    def _append(db, run_id: str, kind: str, data: dict):
        db.execute(
            "INSERT INTO events(run_id, type, data, created_at) VALUES (?, ?, ?, ?)",
            (run_id, kind, json.dumps(data, ensure_ascii=False), now()),
        )

    def event(self, run_id: str, kind: str, data: dict):
        with self.connect() as db:
            self._append(db, run_id, kind, data)

    def transition(self, run_id: str, source: set[str], status: str, *, worker_id: str | None = None,
                   error: str | None = None, output: str | None = None, approval: dict | None = None,
                   event: tuple[str, dict] | None = None):
        conditions = ", ".join("?" for _ in source)
        with self.connect() as db:
            changed = db.execute(
                f"""UPDATE runs SET status = ?, worker_id = COALESCE(?, worker_id), error = ?,
                    output = ?, approval = ?, updated_at = ? WHERE id = ? AND status IN ({conditions})""",
                (status, worker_id, error, output, json.dumps(approval) if approval else None,
                 now(), run_id, *sorted(source)),
            )
            if changed.rowcount != 1:
                raise Conflict("Run state has changed.")
            if event:
                self._append(db, run_id, *event)
            self._append(db, run_id, "status", {"status": status, "error": error})

    def update_approval_mode(self, run_id: str, mode: ApprovalMode) -> tuple[dict, bool]:
        requeued = False
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status, approval_mode, approval FROM runs WHERE id = ?", (run_id,)).fetchone()
            if row is None:
                raise NotFound("Run not found.")
            if row["status"] not in {"queued", "running", "paused", "awaiting_approval"}:
                raise Conflict("Approval mode can only be changed on an unfinished run.")
            if row["approval_mode"] != mode:
                requeued = row["status"] == "awaiting_approval"
                db.execute(
                    "UPDATE runs SET approval_mode = ?, status = ?, approval = ?, updated_at = ? WHERE id = ?",
                    (mode, "queued" if requeued else row["status"], None if requeued else row["approval"], now(), run_id),
                )
                self._append(db, run_id, "approval_mode_changed", {"previous_mode": row["approval_mode"],
                                                                   "approval_mode": mode})
                if requeued:
                    self._append(db, run_id, "status", {"status": "queued", "error": None})
        return self.get(run_id), requeued

    def recover_interrupted(self):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("SELECT id FROM runs WHERE status IN ('running', 'queued')").fetchall()
            reason = "Server restarted. Resume explicitly to continue."
            for row in rows:
                db.execute(
                    "UPDATE runs SET status='paused', error=?, updated_at=? WHERE id=?",
                    (reason, now(), row["id"]),
                )
                self._append(db, row["id"], "status", {"status": "paused", "error": reason})

    def image_path(self, run_id: str, artifact: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{32}\.(?:png|jpg|webp)", artifact):
            raise NotFound("Artifact not found.")
        return self.directory / "artifacts" / run_id / artifact
