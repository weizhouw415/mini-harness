"""Publish skill documents with conflict checks and atomic file replacement."""

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


def _validate_document(name: str, content: str) -> None:
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
def publish_new_skill(directory: Path, name: str, content: str) -> None:
    _validate_document(name, content)
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


def publish_skill_update(directory: Path, name: str, content: str, base_content: str) -> None:
    """Replace an existing document only when it still has the reviewed content."""
    _validate_document(name, content)
    if not isinstance(base_content, str):
        raise SkillPublicationConflict("Missing reviewed version")
    _replace_file(Path(directory), name, "SKILL.md", content, base_content)


def publish_skill_file(directory: Path, name: str, path: str, content: str, base_content: str | None) -> None:
    """Write a supporting file using a fresh read as its precondition."""
    _validate_support(path, content, base_content)
    _replace_file(Path(directory), name, path, content, base_content)


def _validate_support(path: str, content: str, base_content: str | None) -> None:
    if not isinstance(path, str):
        raise ValueError("Invalid supporting file path")
    parts = path.split("/")
    if len(parts) < 2 or parts[0] not in {"references", "templates", "scripts"} or any(
        not part or part in {".", ".."} or "\\" in part or "\x00" in part for part in parts
    ):
        raise ValueError("Invalid supporting file path")
    if not isinstance(content, str) or len(content.encode("utf-8")) > _MAX_BYTES:
        raise ValueError("Invalid supporting file content")
    if base_content is not None and not isinstance(base_content, str):
        raise ValueError("Invalid supporting file precondition")


def preflight_skill(directory: Path, name: str, action: str, content: str,
                    base_content: str | None, files: dict) -> None:
    """Check every intended preimage before publishing any file."""
    _validate_document(name, content)
    if action not in {"create", "update"} or not isinstance(files, dict):
        raise ValueError("Invalid skill changes")
    skill = Path(directory) / name
    if action == "create":
        if skill.exists() or skill.is_symlink():
            raise SkillPublicationConflict("Skill already exists")
    else:
        if not isinstance(base_content, str) or _current_file(skill, "SKILL.md") != base_content:
            raise SkillPublicationConflict("Skill changed since it was read")
    for path, change in files.items():
        if not isinstance(change, dict):
            raise ValueError("Invalid supporting file change")
        new_content, expected = change.get("content"), change.get("base_content")
        _validate_support(path, new_content, expected)
        if action == "create" and expected is not None:
            raise SkillPublicationConflict("New skill cannot replace a supporting file")
        if action == "update" and _current_file(skill, path) != expected:
            raise SkillPublicationConflict("Supporting file changed since it was read")


def _current_file(skill: Path, path: str) -> str | None:
    """Reject symlinks in the reviewed path before examining its preimage."""
    current = skill
    for part in path.split("/"):
        if current.is_symlink():
            raise SkillPublicationConflict("Skill path is a symbolic link")
        current = current / part
    if current.is_symlink():
        raise SkillPublicationConflict("Skill path is a symbolic link")
    try:
        with current.open("r", encoding="utf-8", newline="") as stream:
            return stream.read(_MAX_BYTES + 1)
    except FileNotFoundError:
        return None


def _replace_file(directory: Path, name: str, path: str, content: str, base_content: str | None) -> None:
    if not isinstance(name, str) or len(name) > 64 or not _NAME.fullmatch(name) or name == "versions":
        raise ValueError("Invalid skill name")
    from contextlib import ExitStack

    with ExitStack() as stack:
        root = stack.enter_context(_directory(directory))
        parent = stack.enter_context(_directory(root, name))
        parts = path.split("/")
        for part in parts[:-1]:
            try:
                if os.name == "nt":
                    os.mkdir(parent / part, 0o700)
                else:
                    os.mkdir(part, 0o700, dir_fd=parent)
            except FileExistsError:
                pass
            parent = stack.enter_context(_directory(parent, part))
        target = parts[-1]

        def read_current():
            try:
                flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                fd = os.open(parent / target, flags) if os.name == "nt" else os.open(target, flags, dir_fd=parent)
            except FileNotFoundError:
                return None
            with os.fdopen(fd, "r", encoding="utf-8", newline="") as stream:
                return stream.read(_MAX_BYTES + 1)

        if read_current() != base_content:
            raise SkillPublicationConflict("Skill changed since it was read")
        temporary = ".skill-" + secrets.token_hex(12)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(parent / temporary, flags, 0o600) if os.name == "nt" else os.open(temporary, flags, 0o600, dir_fd=parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            if read_current() != base_content:
                raise SkillPublicationConflict("Skill changed since it was read")
            if os.name == "nt":
                if base_content is None:
                    os.rename(parent / temporary, parent / target)
                else:
                    os.replace(parent / temporary, parent / target)
            elif base_content is None:
                try:
                    os.link(temporary, target, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
                except FileExistsError as error:
                    raise SkillPublicationConflict("Supporting file already exists") from error
                os.unlink(temporary, dir_fd=parent)
            else:
                os.replace(temporary, target, src_dir_fd=parent, dst_dir_fd=parent)
        finally:
            try:
                os.unlink(parent / temporary) if os.name == "nt" else os.unlink(temporary, dir_fd=parent)
            except FileNotFoundError:
                pass
