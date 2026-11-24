"""Parse and store versioned Agent Skills documents."""

import json
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path, PureWindowsPath
from tempfile import TemporaryDirectory
from time import time
from unicodedata import normalize
from uuid import uuid4

import yaml

from harness.artifacts.files import workspace_path

from .matcher import rank_skills

ENVIRONMENTS = frozenset({"cloud-browser", "macos-desktop", "windows-desktop"})
MAX_SKILL_BYTES = 24_000
MAX_WORKSPACE_SKILLS = 32
MAX_WORKSPACE_SKILL_BYTES = 128_000
SKILL_FILE_DIRECTORIES = frozenset({"references", "templates", "scripts", "assets"})
EVALUATION_LEASE_SECONDS = 300
NAME_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
VERSION_PATTERN = re.compile(
    r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
)


class SkillError(ValueError):
    """A skill document or lifecycle operation is invalid."""


def _text_size(content: str, label: str) -> int:
    if not isinstance(content, str):
        raise SkillError(f"{label} must be UTF-8 text no larger than 24 KB")
    try:
        size = len(content.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise SkillError(f"{label} must be valid UTF-8") from exc
    if size > MAX_SKILL_BYTES:
        raise SkillError(f"{label} must be UTF-8 text no larger than 24 KB")
    return size


def _frontmatter(content: str) -> tuple[dict, str]:
    lines = content.strip().splitlines()
    if not lines or lines[0] != "---":
        raise SkillError("SKILL.md must begin with YAML frontmatter")
    try:
        end = lines.index("---", 1)
        fields = yaml.safe_load("\n".join(lines[1:end]))
    except (ValueError, yaml.YAMLError, RecursionError) as exc:
        raise SkillError("SKILL.md has invalid YAML frontmatter") from exc
    body = "\n".join(lines[end + 1 :])
    if not isinstance(fields, dict) or not body.strip():
        raise SkillError("SKILL.md requires frontmatter and instructions")
    return fields, body


def parse_skill(content: str) -> dict:
    _text_size(content, "Skill content")
    fields, _ = _frontmatter(content)
    name = fields.get("name")
    if not isinstance(name, str) or len(name) > 64 or not NAME_PATTERN.fullmatch(name):
        raise SkillError("Skill name must be up to 64 lowercase letters, digits and hyphens")
    description = fields.get("description")
    if not isinstance(description, str) or not description.strip() or len(description) > 1024:
        raise SkillError("Skill description must contain 1–1024 characters")
    try:
        description.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise SkillError("Skill description must be valid UTF-8") from exc
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
        raise SkillError("Skill environments must list supported worker environments: "
                         + ", ".join(sorted(ENVIRONMENTS)))
    return {
        "name": name,
        "description": description.strip(),
        "version": version,
        "version_explicit": "version" in fields or "version" in metadata,
        "environments": sorted(set(environments)),
        "environments_explicit": "environments" in fields,
        "content": content.strip() + "\n",
    }


def _skill_file_path(path: str) -> str:
    try:
        normalized = workspace_path(path)
    except ValueError as error:
        raise SkillError("Skill files require a safe relative path") from error
    directory, slash, _ = normalized.partition("/")
    if not slash or directory not in SKILL_FILE_DIRECTORIES:
        raise SkillError("Skill files belong under references/, templates/, scripts/, or assets/")
    return normalized


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
                CREATE TABLE IF NOT EXISTS direct_versions (
                    name TEXT NOT NULL,
                    version TEXT NOT NULL,
                    PRIMARY KEY (name, version),
                    FOREIGN KEY (name, version) REFERENCES versions (name, version)
                );
                CREATE TABLE IF NOT EXISTS skill_files (
                    name TEXT NOT NULL,
                    version TEXT NOT NULL,
                    path TEXT NOT NULL,
                    content TEXT NOT NULL,
                    PRIMARY KEY (name, version, path),
                    FOREIGN KEY (name, version) REFERENCES versions (name, version)
                );
                """
            )
        with self._connection(write=True) as db:
            if db.execute("PRAGMA user_version").fetchone()[0] == 0:
                db.execute("""
                    INSERT INTO direct_versions (name, version)
                    SELECT name, version FROM (
                        SELECT name, version, row_number() OVER (
                            PARTITION BY name ORDER BY created_at DESC, version DESC
                        ) AS position FROM versions AS draft
                        WHERE state = 'candidate' AND NOT EXISTS (
                            SELECT 1 FROM versions AS published
                            WHERE published.name = draft.name AND published.state = 'active'
                        )
                    ) WHERE position = 1
                """)
                db.execute("""
                    UPDATE versions SET state = 'active'
                    WHERE (name, version) IN (SELECT name, version FROM direct_versions)
                """)
                db.execute("PRAGMA user_version = 1")

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
            if not self._unused_version(db, name, version):
                raise SkillError(f"Skill {name} version {version} already exists")
            return self._insert_version(db, skill)

    def _insert_version(self, db: sqlite3.Connection, skill: dict, *, files: dict | None = None) -> dict:
        name, version = skill["name"], skill["version"]
        state = "candidate" if files is None else "active"
        if files is not None:
            db.execute("UPDATE versions SET state = 'deprecated' WHERE name = ? AND state = 'active'", (name,))
        db.execute(
            "INSERT INTO versions VALUES (?, ?, ?, ?, ?, ?, ?)",
            (name, version, skill["description"], json.dumps(skill["environments"]), skill["content"], state, time()),
        )
        if files is not None:
            db.execute("INSERT INTO direct_versions VALUES (?, ?)", (name, version))
            db.executemany("INSERT INTO skill_files VALUES (?, ?, ?, ?)",
                           [(name, version, path, content) for path, content in files.items()])
        directory = self._document(name, version).parent
        directory.parent.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(dir=directory.parent) as temporary:
            staging = Path(temporary) / "version"
            for path, content in {"SKILL.md": skill["content"], **(files or {})}.items():
                destination = staging / path
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(content, encoding="utf-8", newline="\n")
            os.replace(staging, directory)
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
        available: bool | None = None,
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
            if available is not None:
                record["available"] = available
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
            if environment is None:
                return [self._record(db, row) for row in rows]
            available = {(row["name"], row["version"]) for row in self._active_entries(db, environment)}
            return [self._record(db, row, environment=environment, available=(row["name"], row["version"]) in available)
                    for row in rows]

    @staticmethod
    def _active_entries(db: sqlite3.Connection, environment: str | None,
                        *, name: str | None = None) -> list[sqlite3.Row]:
        return db.execute(
            """
            SELECT v.name, v.version, v.description, v.environments, v.state, v.created_at
            FROM versions AS v LEFT JOIN direct_versions AS direct USING (name, version)
            WHERE v.state = 'active' AND (? IS NULL OR v.name = ?)
            AND (? IS NULL OR (
                direct.name IS NOT NULL AND (json_array_length(v.environments) = 0 OR ? IN (
                    SELECT value FROM json_each(v.environments)
                ))
            ) OR (
                direct.name IS NULL AND EXISTS (
                    SELECT 1 FROM evaluations AS e
                    WHERE e.name = v.name AND e.version = v.version AND e.environment = ? AND e.status = 'passed'
                )
            ))
            ORDER BY v.name
            """,
            (name, name, environment, environment, environment),
        ).fetchall()

    def active_index(self, environment: str | None = None) -> list[dict]:
        if environment is not None:
            self._environment(environment)
        with self._connection() as db:
            return [self._record(db, row, environment=environment, available=True)
                    for row in self._active_entries(db, environment)]

    def _read_active(self, db: sqlite3.Connection, name: str, environment: str | None) -> dict:
        if environment is not None:
            self._environment(environment)
        if not self._active_entries(db, environment, name=name):
            raise SkillError("No active skill with this name is available in this environment")
        return self._get(db, name, None)

    @staticmethod
    def _files(db: sqlite3.Connection, name: str, version: str) -> dict[str, str]:
        return dict(db.execute(
            "SELECT path, content FROM skill_files WHERE name = ? AND version = ? ORDER BY path", (name, version),
        ))

    def view_active(self, name: str, environment: str | None = None, path: str | None = None) -> dict:
        with self._connection() as db:
            skill = self._read_active(db, name, environment)
            record = {"name": name, "version": skill["version"]}
            files = self._files(db, name, skill["version"])
            if path is None or path == "SKILL.md":
                return {**record, "document": skill["content"], "files": list(files)}
            path = _skill_file_path(path)
            if path not in files:
                raise SkillError("Skill file does not exist")
            return {**record, "path": path, "document": files[path]}

    def _check_base(self, db: sqlite3.Connection, name: str, base_version: str, environment: str | None) -> dict:
        current = self._read_active(db, name, environment)
        if current["version"] != base_version:
            raise SkillError(f"Skill has changed to version {current['version']}. Read it again before editing.")
        return current

    def _unused_version(self, db: sqlite3.Connection, name: str, version: str) -> bool:
        return (db.execute("SELECT 1 FROM versions WHERE name = ? AND version = ?", (name, version)).fetchone() is None
                and not self._document(name, version).parent.exists())

    @staticmethod
    def _is_direct(db: sqlite3.Connection, name: str, version: str) -> bool:
        record = db.execute("SELECT 1 FROM direct_versions WHERE name = ? AND version = ?", (name, version)).fetchone()
        return record is not None

    def _save_revision(self, db: sqlite3.Connection, skill: dict, current: dict | None = None,
                       *, files: dict[str, str] | None = None) -> dict:
        name, version = skill["name"], skill["version"]
        if current is not None and current["content"] == skill["content"] and files is None:
            return current
        fields_to_update = {}
        if current is not None and not skill["environments_explicit"]:
            if self._is_direct(db, name, current["version"]):
                environments = parse_skill(current["content"])["environments"]
            else:
                environments = sorted(env for env, result in current["evaluations"].items() if result["status"] == "passed")
            if environments:
                fields_to_update["environments"] = environments
        if (current is not None and not skill["version_explicit"]) or not self._unused_version(db, name, version):
            base = current["version"] if current is not None else version
            major, minor, patch = (int(part) for part in base.split("-", 1)[0].split("."))
            while True:
                patch += 1
                version = f"{major}.{minor}.{patch}"
                if len(version) > 64:
                    raise SkillError("Supply a shorter unused semantic version")
                if self._unused_version(db, name, version):
                    break
            fields_to_update["version"] = version
        if fields_to_update:
            fields, body = _frontmatter(skill["content"])
            fields.update(fields_to_update)
            skill = parse_skill("---\n" + yaml.safe_dump(fields, allow_unicode=True, sort_keys=False) + "---\n" + body)
        if files is None:
            files = self._files(db, name, current["version"]) if current is not None else {}
        return self._insert_version(db, skill, files=files)

    def save_active(self, content: str, *, name: str | None = None, base_version: str | None = None,
                    environment: str | None = None, allow_update: bool = False) -> dict:
        if environment is not None:
            self._environment(environment)
        skill = parse_skill(content)
        if name is not None and skill["name"] != name:
            raise SkillError("Skill name must match SKILL.md frontmatter")
        name = skill["name"]
        with self._connection(write=True) as db:
            current = None
            if base_version is not None:
                current = self._check_base(db, name, base_version, environment)
            elif self._active_entries(db, None, name=name):
                if not allow_update:
                    raise SkillError("An active skill with this name already exists. Read it before editing.")
                current = self._read_active(db, name, environment)
            return self._save_revision(db, skill, current)

    def patch_active(self, name: str, base_version: str, old_string: str, new_string: str,
                     *, environment: str | None = None) -> dict:
        _text_size(old_string, "Skill patch search text")
        _text_size(new_string, "Skill patch replacement")
        if not old_string:
            raise SkillError("Skill patch search text must be nonempty")
        with self._connection(write=True) as db:
            current = self._check_base(db, name, base_version, environment)
            content = current["content"]
            position = content.find(old_string)
            if position < 0 or content.find(old_string, position + 1) >= 0:
                raise SkillError("Skill patch search text must match exactly once")
            skill = parse_skill(content.replace(old_string, new_string, 1))
            if skill["name"] != name:
                raise SkillError("A skill edit cannot change its name")
            return self._save_revision(db, skill, current)

    def write_skill_file(self, name: str, base_version: str, path: str, content: str,
                         *, environment: str | None = None) -> dict:
        path = _skill_file_path(path)
        _text_size(content, "Skill file")
        with self._connection(write=True) as db:
            current = self._check_base(db, name, base_version, environment)
            files = self._files(db, name, base_version)
            if path in files and files[path] == content:
                return current
            target = normalize("NFC", path).casefold()
            for existing in files.keys() - {path}:
                other = normalize("NFC", existing).casefold()
                if other == target or other.startswith(target + "/") or target.startswith(other + "/"):
                    raise SkillError("Skill file path collides with an existing file")
            files[path] = content
            size = sum(len(text.encode("utf-8")) for text in files.values())
            if len(files) > MAX_WORKSPACE_SKILLS or size > MAX_WORKSPACE_SKILL_BYTES:
                raise SkillError("A skill can contain up to 32 supporting files totaling 128 KB")
            return self._save_revision(db, parse_skill(current["content"]), current, files=files)

    def match(self, query: str, environment: str, *, limit: int = 3) -> list[dict]:
        self._environment(environment)
        if limit < 1:
            return []
        with self._connection() as db:
            return [
                self._get(db, skill["name"], skill["version"], environment=environment)
                for skill in rank_skills(query, self._active_entries(db, environment))[:limit]
            ]

    def workspace_snapshot(self, environment: str, query: str) -> list[dict]:
        """Export applicable active skill documents for Bash discovery on one worker."""
        self._environment(environment)
        with self._connection() as db:
            eligible = [dict(row) for row in self._active_entries(db, environment)]
            matches = rank_skills(query, eligible)
            matched_names = {item["name"] for item in matches}
            snapshot = []
            remaining = MAX_WORKSPACE_SKILL_BYTES
            for item in matches + [item for item in eligible if item["name"] not in matched_names]:
                if len(snapshot) == MAX_WORKSPACE_SKILLS:
                    break
                if environment == "windows-desktop" and PureWindowsPath(item["name"]).is_reserved():
                    continue
                content = db.execute(
                    "SELECT content FROM versions WHERE name = ? AND version = ?",
                    (item["name"], item["version"]),
                ).fetchone()["content"]
                skill = {key: item[key] for key in ("name", "version", "description")}
                skill["content"] = content
                size = sum(len(value.encode("utf-8")) for value in skill.values())
                if size <= remaining:
                    snapshot.append(skill)
                    remaining -= size
            return snapshot

    def import_workspace_candidates(self, candidates: list) -> dict[str, int]:
        """Validate and activate legacy drafts staged in worker run directories."""
        if not isinstance(candidates, list):
            return {"accepted": 0, "rejected": 1}
        accepted = 0
        remaining = MAX_WORKSPACE_SKILL_BYTES
        for item in candidates[:MAX_WORKSPACE_SKILLS]:
            if not isinstance(item, dict):
                continue
            try:
                skill = parse_skill(item.get("content"))
                size = len(skill["content"].encode("utf-8"))
                if skill["name"] != item.get("name") or size > remaining:
                    continue
                remaining -= size
                with self._connection(write=True) as db:
                    name, version = skill["name"], skill["version"]
                    existing = db.execute("SELECT content FROM versions WHERE name = ? AND version = ?",
                                          (name, version)).fetchone()
                    if existing is not None:
                        if existing["content"] != skill["content"]:
                            raise SkillError(f"Skill {name} version {version} already exists")
                    else:
                        current = self._get(db, name, None) if self._active_entries(db, None, name=name) else None
                        self._save_revision(db, skill, current)
            except SkillError:
                continue
            accepted += 1
        return {"accepted": accepted, "rejected": len(candidates) - accepted}

    def _activate(self, name: str, version: str, *, approved: bool, from_state: str) -> dict:
        if approved is not True:
            raise SkillError("Activation requires explicit approval")
        with self._connection(write=True) as db:
            skill = self._get(db, name, version)
            if skill["state"] != from_state:
                raise SkillError(f"Expected a {from_state} skill version")
            direct = from_state == "deprecated" and self._is_direct(db, name, version)
            if not direct and not any(result["status"] == "passed" for result in skill["evaluations"].values()):
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
