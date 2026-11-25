"""Import active skills from the old catalog once during an upgrade."""

import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from harness.artifacts.files import workspace_path


def migrate_skill_catalog(source: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    marker = destination / ".catalog-imported"
    catalog = source / "catalog.sqlite"
    if marker.exists() or not catalog.is_file():
        return

    with closing(sqlite3.connect(catalog.resolve().as_uri() + "?mode=ro", uri=True)) as db, db:
        db.execute("BEGIN")
        active = db.execute("SELECT name, version, content FROM versions WHERE state = 'active'").fetchall()
        has_files = db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'skill_files'").fetchone()
        for name, version, content in active:
            if workspace_path(name) != name or "/" in name or name == "versions":
                raise ValueError("Invalid skill name in legacy catalog")
            target = destination / name
            if target.exists():
                continue
            with TemporaryDirectory(dir=destination) as temporary:
                staging = Path(temporary) / name
                staging.mkdir()
                (staging / "SKILL.md").write_text(content, encoding="utf-8")
                files = db.execute(
                    "SELECT path, content FROM skill_files WHERE name = ? AND version = ?", (name, version),
                ) if has_files else ()
                for path, text in files:
                    parts = workspace_path(path).split("/")
                    if len(parts) < 2 or parts[0] not in {"references", "templates", "scripts", "assets"}:
                        raise ValueError("Invalid supporting file in legacy catalog")
                    document = staging.joinpath(*parts)
                    document.parent.mkdir(parents=True, exist_ok=True)
                    document.write_text(text, encoding="utf-8")
                staging.rename(target)
    marker.touch()
