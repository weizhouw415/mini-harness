"""Parse and store versioned Agent Skills documents."""

import json
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from time import time
from uuid import uuid4

import yaml

from .matcher import rank_skills

ENVIRONMENTS = frozenset({"cloud-browser", "macos-desktop", "windows-desktop"})
MAX_SKILL_BYTES = 24_000
EVALUATION_LEASE_SECONDS = 300
NAME_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
VERSION_PATTERN = re.compile(
    r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
)


class SkillError(ValueError):
    """A skill document or lifecycle operation is invalid."""


def parse_skill(content: str) -> dict:
    if not isinstance(content, str):
        raise SkillError("Skill content must be UTF-8 text no larger than 24 KB")
    try:
        size = len(content.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise SkillError("Skill content must be valid UTF-8") from exc
    if size > MAX_SKILL_BYTES:
        raise SkillError("Skill content must be UTF-8 text no larger than 24 KB")
    lines = content.strip().splitlines()
    if not lines or lines[0] != "---":
        raise SkillError("SKILL.md must begin with YAML frontmatter")
    try:
        end = lines.index("---", 1)
        fields = yaml.safe_load("\n".join(lines[1:end]))
    except (ValueError, yaml.YAMLError, RecursionError) as exc:
        raise SkillError("SKILL.md has invalid YAML frontmatter") from exc
    if not isinstance(fields, dict) or not "\n".join(lines[end + 1 :]).strip():
        raise SkillError("SKILL.md requires frontmatter and instructions")

    name = fields.get("name")
    if not isinstance(name, str) or len(name) > 64 or not NAME_PATTERN.fullmatch(name):
        raise SkillError("Skill name must be up to 64 lowercase letters, digits and hyphens")
    description = fields.get("description")
    if not isinstance(description, str) or not description.strip() or len(description) > 1024:
        raise SkillError("Skill description must contain 1–1024 characters")
    metadata = fields.get("metadata", {})
    if not isinstance(metadata, dict):
        raise SkillError("Skill metadata must be a mapping")
    version = fields.get("version", metadata.get("version", "0.1.0"))
    if not isinstance(version, str) or len(version) > 64 or not VERSION_PATTERN.fullmatch(version):
        raise SkillError("Skill version must use semantic versioning, for example 1.0.0")
    environments = fields.get("environments", [])
    if not isinstance(environments, list) or any(
        not isinstance(item, str) or item not in ENVIRONMENTS for item in environments
    ):
        raise SkillError("Skill environments must list supported worker environments")
    return {
        "name": name,
        "description": description.strip(),
        "version": version,
        "environments": sorted(set(environments)),
        "content": content.strip() + "\n",
    }


class SkillStore:
    def __init__(self, path: str | Path):
        self.path = Path(path).resolve()
        self.path.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.database = self.path / "catalog.sqlite"
        with self._connection() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS versions (
                    name TEXT NOT NULL,
                    version TEXT NOT NULL,
                    description TEXT NOT NULL,
                    environments TEXT NOT NULL,
                    content TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (state IN ('candidate', 'active', 'deprecated')),
                    created_at REAL NOT NULL,
                    PRIMARY KEY (name, version)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_version
                    ON versions (name) WHERE state = 'active';
                CREATE TABLE IF NOT EXISTS evaluations (
                    name TEXT NOT NULL,
                    version TEXT NOT NULL,
                    environment TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('testing', 'passed', 'failed')),
                    details TEXT NOT NULL,
                    token TEXT NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (name, version, environment),
                    FOREIGN KEY (name, version) REFERENCES versions (name, version)
                );
                """
            )

    @contextmanager
    def _connection(self, *, write: bool = False):
        db = sqlite3.connect(self.database, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys = ON")
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db
            db.commit()
        finally:
            db.close()

    def _document(self, name: str, version: str) -> Path:
        return self.path / "versions" / name / version / "SKILL.md"

    def create_candidate(self, content: str) -> dict:
        skill = parse_skill(content)
        name, version = skill["name"], skill["version"]
        with self._connection(write=True) as db:
            if db.execute(
                "SELECT 1 FROM versions WHERE name = ? AND version = ?", (name, version)
            ).fetchone():
                raise SkillError(f"Skill {name} version {version} already exists")
            db.execute(
                """
                INSERT INTO versions VALUES (?, ?, ?, ?, ?, 'candidate', ?)
                """,
                (
                    name,
                    version,
                    skill["description"],
                    json.dumps(skill["environments"]),
                    skill["content"],
                    time(),
                ),
            )
            document = self._document(name, version)
            document.parent.mkdir(parents=True, exist_ok=True)
            temporary_path = None
            try:
                with NamedTemporaryFile(
                    mode="w", encoding="utf-8", dir=document.parent, delete=False
                ) as temporary:
                    temporary_path = Path(temporary.name)
                    temporary.write(skill["content"])
                os.replace(temporary_path, document)
            finally:
                if temporary_path is not None:
                    temporary_path.unlink(missing_ok=True)
            return self._get(db, name, version)

    @staticmethod
    def _environment(environment: str) -> None:
        if not isinstance(environment, str) or environment not in ENVIRONMENTS:
            raise SkillError(f"Unknown worker environment: {environment}")

    def _record(
        self,
        db: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        environment: str | None = None,
        include_content: bool = False,
    ) -> dict:
        name, version = row["name"], row["version"]
        declared = json.loads(row["environments"])
        evaluations = {
            evaluation["environment"]: {**json.loads(evaluation["details"]), "status": evaluation["status"]}
            for evaluation in db.execute(
                """
                SELECT environment, status, details FROM evaluations
                WHERE name = ? AND version = ?
                      AND (status != 'testing' OR updated_at > ?)
                ORDER BY environment
                """,
                (name, version, time() - EVALUATION_LEASE_SECONDS),
            )
        }
        state = row["state"]
        if state == "candidate" and any(result["status"] == "testing" for result in evaluations.values()):
            state = "testing"
        record = {
            "name": name,
            "version": version,
            "description": row["description"],
            "state": state,
            "environments": sorted(set(declared) | evaluations.keys()),
            "evaluations": evaluations,
            "created_at": datetime.fromtimestamp(row["created_at"], UTC).isoformat(),
        }
        if include_content:
            record["content"] = row["content"]
        if environment is not None:
            record["environment_status"] = evaluations.get(environment, {}).get("status", "untested")
        return record

    def _get(self, db: sqlite3.Connection, name: str, version: str | None, *, environment=None) -> dict:
        if version is None:
            row = db.execute(
                """
                SELECT * FROM versions WHERE name = ?
                ORDER BY state = 'active' DESC, created_at DESC LIMIT 1
                """,
                (name,),
            ).fetchone()
        else:
            row = db.execute(
                "SELECT * FROM versions WHERE name = ? AND version = ?", (name, version)
            ).fetchone()
        if row is None:
            raise SkillError("Skill version does not exist")
        return self._record(db, row, environment=environment, include_content=True)

    def get(self, name: str, version: str | None = None) -> dict:
        with self._connection() as db:
            return self._get(db, name, version)

    def index(self, environment: str | None = None) -> list[dict]:
        if environment is not None:
            self._environment(environment)
        with self._connection() as db:
            rows = db.execute(
                "SELECT name, version, description, environments, state, created_at "
                "FROM versions ORDER BY name, created_at DESC"
            )
            return [self._record(db, row, environment=environment) for row in rows]

    def match(self, query: str, environment: str, *, limit: int = 3) -> list[dict]:
        self._environment(environment)
        if limit < 1:
            return []
        with self._connection() as db:
            rows = db.execute(
                """
                SELECT name, version, description FROM versions JOIN evaluations USING (name, version)
                WHERE state = 'active' AND environment = ? AND status = 'passed'
                ORDER BY name
                """,
                (environment,),
            )
            return [
                self._get(db, skill["name"], skill["version"], environment=environment)
                for skill in rank_skills(query, [dict(row) for row in rows])[:limit]
            ]

    def _activate(self, name: str, version: str, *, approved: bool, from_state: str) -> dict:
        if approved is not True:
            raise SkillError("Activation requires explicit approval")
        with self._connection(write=True) as db:
            skill = self._get(db, name, version)
            if skill["state"] != from_state:
                raise SkillError(f"Expected a {from_state} skill version")
            if not any(result["status"] == "passed" for result in skill["evaluations"].values()):
                raise SkillError("Activation requires a passing evaluation")
            db.execute("UPDATE versions SET state = 'deprecated' WHERE name = ? AND state = 'active'", (name,))
            db.execute("UPDATE versions SET state = 'active' WHERE name = ? AND version = ?", (name, version))
            return self._get(db, name, version)

    def publish(self, name: str, version: str, *, approved: bool) -> dict:
        return self._activate(name, version, approved=approved, from_state="candidate")

    def rollback(self, name: str, version: str, *, approved: bool) -> dict:
        return self._activate(name, version, approved=approved, from_state="deprecated")

    def start_evaluation(self, name: str, version: str, environment: str) -> tuple[dict, str]:
        self._environment(environment)
        token = uuid4().hex
        with self._connection(write=True) as db:
            skill = self._get(db, name, version, environment=environment)
            if skill["state"] == "deprecated":
                raise SkillError("Deprecated versions are evaluated before publication")
            if skill["environment_status"] in {"testing", "passed"}:
                raise SkillError("This version already has a pending or passing evaluation in this environment")
            db.execute(
                """
                INSERT INTO evaluations VALUES (?, ?, ?, 'testing', '{}', ?, ?)
                ON CONFLICT (name, version, environment) DO UPDATE SET
                    status = excluded.status, details = excluded.details,
                    token = excluded.token, updated_at = excluded.updated_at
                """,
                (name, version, environment, token, time()),
            )
            return self._get(db, name, version, environment=environment), token

    def finish_evaluation(
        self, name: str, version: str, environment: str, token: str, result: dict
    ) -> dict:
        if not isinstance(result, dict) or type(result.get("passed")) is not bool:
            raise SkillError("Evaluator must return a boolean passed field")
        try:
            details = json.dumps(result, allow_nan=False)
        except (ValueError, TypeError, RecursionError) as exc:
            raise SkillError("Evaluator result must be JSON serializable") from exc
        if len(details) > 16_000:
            raise SkillError("Evaluator result exceeds 16 KB")
        with self._connection(write=True) as db:
            change = db.execute(
                """
                UPDATE evaluations SET status = ?, details = ?, updated_at = ?
                WHERE name = ? AND version = ? AND environment = ? AND token = ? AND status = 'testing'
                """,
                ("passed" if result["passed"] else "failed", details, time(), name, version, environment, token),
            )
            if change.rowcount != 1:
                raise SkillError("Evaluation is no longer pending")
            return self._get(db, name, version, environment=environment)
