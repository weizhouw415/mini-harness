"""Discover skill names and descriptions without loading instructions into context."""

from pathlib import Path
import re

import yaml


_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_MAX_HEADER_BYTES = 8192


def skill_profiles(directory: Path, environment: str) -> list[dict[str, str]]:
    profiles = []
    try:
        entries = sorted(directory.iterdir())
    except OSError:
        return profiles
    for entry in entries:
        if not _NAME.fullmatch(entry.name) or entry.name == "versions":
            continue
        document = entry / "SKILL.md"
        try:
            if entry.is_symlink() or document.is_symlink() or not document.is_file():
                continue
            with document.open("rb") as file:
                if file.readline(_MAX_HEADER_BYTES).strip() != b"---":
                    continue
                header = bytearray()
                while line := file.readline(_MAX_HEADER_BYTES - len(header)):
                    if line.strip() == b"---" and line.endswith((b"\n", b"\r")):
                        break
                    header.extend(line)
                else:
                    continue
                metadata = yaml.safe_load(header.decode("utf-8"))
        except (OSError, UnicodeError, yaml.YAMLError, RecursionError):
            continue
        if not isinstance(metadata, dict) or metadata.get("name") != entry.name:
            continue
        environments = metadata.get("environments", [])
        description = metadata.get("description")
        if not isinstance(environments, list) or (environments and environment not in environments):
            continue
        if isinstance(description, str) and description.strip():
            profiles.append({"name": entry.name, "description": description.strip()[:1024],
                             "path": document.as_posix()})
        if len(profiles) == 32:
            break
    return profiles
