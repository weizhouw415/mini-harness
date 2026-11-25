"""Publish a new file-based skill without replacing an existing one."""

import os
import re
import secrets
from pathlib import Path

import yaml

from workers.common.filesystem import _directory

_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_MAX_BYTES = 64 * 1024


class SkillPublicationConflict(Exception):
    """A skill with this name already exists."""


def publish_new_skill(directory: Path, name: str, content: str) -> None:
    if not isinstance(name, str) or len(name) > 64 or not _NAME.fullmatch(name) or name == "versions":
        raise ValueError("Invalid skill name")
    if not isinstance(content, str) or len(content.encode("utf-8")) > _MAX_BYTES:
        raise ValueError("Invalid skill content")
    parts = content.split("\n", 2)
    if len(parts) != 3 or parts[0].strip() != "---" or "\n---" not in "\n" + parts[2]:
        raise ValueError("Skill must contain YAML frontmatter")
    try:
        header = content.split("\n---", 1)[0][4:]
        metadata = yaml.safe_load(header)
    except (yaml.YAMLError, RecursionError) as error:
        raise ValueError("Invalid skill frontmatter") from error
    if (not isinstance(metadata, dict) or metadata.get("name") != name
            or not isinstance(metadata.get("description"), str) or not metadata["description"].strip()):
        raise ValueError("Skill frontmatter must match name and include description")
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Open the configured root without following a symlink. Exclusive mkdir is
    # the concurrency guard; never replace an existing skill or a symlink.
    with _directory(directory) as root:
        try:
            if os.name == "nt":
                os.mkdir(root / name, 0o700)
            else:
                os.mkdir(name, 0o700, dir_fd=root)
        except FileExistsError as error:
            raise SkillPublicationConflict(name) from error
        try:
            with _directory(root, name) as skill:
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
                temporary = ".skill-" + secrets.token_hex(12)
                if os.name == "nt":
                    fd = os.open(skill / temporary, flags, 0o600)
                else:
                    fd = os.open(temporary, flags, 0o600, dir_fd=skill)
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                if os.name == "nt":
                    os.rename(skill / temporary, skill / "SKILL.md")
                else:
                    os.link(temporary, "SKILL.md", src_dir_fd=skill, dst_dir_fd=skill, follow_symlinks=False)
                    os.unlink(temporary, dir_fd=skill)
        except BaseException:
            # Do not remove paths that may have been replaced concurrently.
            # A failed empty directory is harmless and blocks accidental overwrite.
            raise
