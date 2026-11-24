from __future__ import annotations

import json
import re
import sqlite3
import tempfile
from contextlib import contextmanager
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Literal
from uuid import uuid4

from harness.artifacts.files import MAX_FILE_BYTES, MAX_UPLOAD_BYTES, MAX_UPLOADS, file_mime, workspace_path


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
                    worker_target TEXT CHECK(worker_target IN ('auto', 'browser', 'desktop')),
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
                CREATE TABLE IF NOT EXISTS run_files (
                    id TEXT PRIMARY KEY,
                    run_id TEXT REFERENCES runs(id),
                    source TEXT NOT NULL CHECK(source IN ('upload', 'output', 'preview')),
                    name TEXT NOT NULL,
                    path TEXT NOT NULL,
                    mime_type TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    legacy_name TEXT
                );
                CREATE INDEX IF NOT EXISTS run_file_paths ON run_files(run_id, source, path, version);
            """)
            db.execute("BEGIN IMMEDIATE")
            columns = {row["name"] for row in db.execute("PRAGMA table_info(runs)")}
            if "approval_mode" not in columns:
                db.execute("""
                    ALTER TABLE runs ADD COLUMN approval_mode TEXT NOT NULL DEFAULT 'legacy'
                        CHECK(approval_mode IN ('legacy', 'manual', 'auto', 'allow'))
                """)
            if "worker_target" not in columns:
                db.execute("""
                    ALTER TABLE runs ADD COLUMN worker_target TEXT
                        CHECK(worker_target IN ('auto', 'browser', 'desktop'))
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
               approval_mode: ApprovalMode = "legacy", *, upload_ids: list[str] | None = None,
               worker_target: str | None = None) -> dict:
        run_id, workspace_id = uuid4().hex, uuid4().hex
        created = now()
        upload_ids = upload_ids or []
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            upload_bytes = 0
            if len(upload_ids) > MAX_UPLOADS:
                raise Conflict("Attach at most 8 files to a run.")
            if len(set(upload_ids)) != len(upload_ids):
                raise Conflict("Each upload can only be attached once to a run.")
            for upload_id in upload_ids:
                upload = db.execute(
                    "SELECT run_id, size FROM run_files WHERE id = ? AND source = 'upload'", (upload_id,),
                ).fetchone()
                if upload is None:
                    raise NotFound("Upload not found.")
                if upload["run_id"] is not None:
                    raise Conflict("This upload is already attached to a run.")
                upload_bytes += upload["size"]
            if upload_bytes > MAX_UPLOAD_BYTES:
                raise Conflict("Total attachments exceed 40 MiB.")
            if session_id is None:
                session_id = uuid4().hex
                db.execute("INSERT INTO sessions VALUES (?, ?)", (session_id, created))
            elif not db.execute("SELECT 1 FROM sessions WHERE id = ?", (session_id,)).fetchone():
                raise NotFound("Session not found.")
            try:
                db.execute(
                    """INSERT INTO runs (id, session_id, requested_worker, worker_target, workspace_id, task,
                       status, approval_mode, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?)""",
                    (run_id, session_id, worker_id, worker_target, workspace_id, task, approval_mode, created, created),
                )
            except sqlite3.IntegrityError as error:
                raise Conflict("This session already has an unfinished run.") from error
            db.executemany("UPDATE run_files SET run_id = ? WHERE id = ?",
                           [(run_id, upload_id) for upload_id in upload_ids])
        return self.get(run_id)

    def get(self, run_id: str, *, include_files: bool = False) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            if row is None:
                raise NotFound("Run not found.")
            run = dict(row)
            run["approval"] = json.loads(run["approval"]) if run["approval"] else None
            if include_files:
                run["files"] = self._run_files(db, run_id)
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
                    """SELECT seq, type, data, created_at FROM events
                       WHERE run_id = ? AND type != 'assistant_delta' ORDER BY seq""",
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
            return [
                {**row, "approval": json.loads(row["approval"]) if row["approval"] else None,
                 "files": self._run_files(db, row["id"])}
                for row in rows
            ]

    @staticmethod
    def _file_metadata(row) -> dict:
        return {key: row[key] for key in ("id", "source", "name", "path", "mime_type",
                                          "size", "sha256", "version", "created_at")}

    @classmethod
    def _run_files(cls, db, run_id: str) -> list[dict]:
        rows = db.execute("SELECT * FROM run_files WHERE run_id = ? ORDER BY rowid", (run_id,))
        return [cls._file_metadata(row) for row in rows]

    def files(self, run_id: str) -> list[dict]:
        return self.get(run_id, include_files=True)["files"]

    def run_file(self, run_id: str, file_id: str) -> tuple[dict, Path]:
        with self.connect() as db:
            row = db.execute("SELECT * FROM run_files WHERE run_id = ? AND id = ?", (run_id, file_id)).fetchone()
        if row is None:
            raise NotFound("File not found.")
        path = (self.image_path(run_id, row["legacy_name"]) if row["legacy_name"]
                else self.directory / "files" / row["sha256"])
        if not path.is_file():
            raise NotFound("File content not found.")
        return self._file_metadata(row), path

    @staticmethod
    def _write_file(path: Path, content: bytes):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".file-", delete=False) as target:
            temporary = Path(target.name)
            try:
                target.write(content)
                target.close()
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)

    def add_upload(self, name: str, content: bytes) -> dict:
        return self._save_file(None, "upload", name, content, file_mime(name))

    def add_run_file(self, run_id: str, source: Literal["output", "preview"], path: str,
                     content: bytes, *, mime_type: str | None = None, legacy_name: str | None = None) -> dict:
        return self._save_file(run_id, source, path, content, mime_type, legacy_name)

    def _save_file(self, run_id: str | None, source: Literal["upload", "output", "preview"],
                   path: str, content: bytes, mime_type: str | None, legacy_name: str | None = None) -> dict:
        if len(content) > MAX_FILE_BYTES:
            raise ValueError("File exceeds 20 MiB.")
        path = workspace_path(path)
        name = path.rsplit("/", 1)[-1]
        identifier, digest = uuid4().hex, sha256(content).hexdigest()
        if source == "upload":
            path = f"uploads/{identifier}/{name}"
        destination = (self.image_path(run_id, legacy_name) if legacy_name
                       else self.directory / "files" / digest)
        if not destination.is_file():
            self._write_file(destination, content)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            latest = db.execute(
                """SELECT * FROM run_files WHERE run_id = ? AND source = ? AND path = ?
                   ORDER BY version DESC LIMIT 1""", (run_id, source, path),
            ).fetchone()
            if latest and latest["sha256"] == digest:
                return self._file_metadata(latest)
            version = latest["version"] + 1 if latest else 1
            db.execute("INSERT INTO run_files VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                       (identifier, run_id, source, name, path, mime_type or file_mime(name), len(content),
                        digest, version, now(), legacy_name))
            return self._file_metadata(db.execute("SELECT * FROM run_files WHERE id = ?", (identifier,)).fetchone())

    def events(self, run_id: str, *, after: int = 0, include_deltas: bool = True) -> list[dict]:
        with self.connect() as db:
            if not db.execute("SELECT 1 FROM runs WHERE id = ?", (run_id,)).fetchone():
                raise NotFound("Run not found.")
            rows = db.execute(
                """SELECT seq, type, data, created_at FROM events
                   WHERE run_id = ? AND seq > ? AND (? OR type != 'assistant_delta') ORDER BY seq""",
                (run_id, after, include_deltas),
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
