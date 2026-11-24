"""Seed the run's skill snapshot and collect only bounded, regular candidate documents."""

import json
import os
import shutil
import stat
from contextlib import contextmanager
from itertools import islice
from pathlib import Path, PureWindowsPath

from harness.skills.loader import (
    MAX_SKILL_BYTES,
    MAX_WORKSPACE_SKILL_BYTES,
    MAX_WORKSPACE_SKILLS,
    NAME_PATTERN,
    VERSION_PATTERN,
)

ROOT = ".mini-harness"
SKILL_FILE = "SKILL.md"


def _valid_name(name) -> bool:
    return (
        isinstance(name, str)
        and len(name) <= 64
        and NAME_PATTERN.fullmatch(name) is not None
        and (os.name != "nt" or not PureWindowsPath(name).is_reserved())
    )


def prepare_skill_workspace(workspace: Path, skills: list[dict]) -> None:
    if not isinstance(skills, list) or len(skills) > MAX_WORKSPACE_SKILLS:
        raise ValueError("Invalid skill snapshot")
    seen = set()
    total_size = 0
    for item in skills:
        if not isinstance(item, dict):
            raise ValueError("Invalid skill snapshot")
        name, version = item.get("name"), item.get("version")
        description, content = item.get("description"), item.get("content")
        if (
            not _valid_name(name) or name in seen
            or not isinstance(version, str) or len(version) > 64 or not VERSION_PATTERN.fullmatch(version)
            or not isinstance(description, str) or not description.strip() or len(description) > 1024
            or not isinstance(content, str) or len(content.encode("utf-8")) > MAX_SKILL_BYTES + 1
        ):
            raise ValueError("Invalid skill snapshot")
        seen.add(name)
        total_size += sum(len(value.encode("utf-8")) for value in (name, version, description, content))
    if total_size > MAX_WORKSPACE_SKILL_BYTES:
        raise ValueError("Skill snapshot exceeds its size limit")

    root = workspace / ROOT
    root.mkdir(mode=0o700)
    try:
        skills_dir = root / "skills"
        active = skills_dir / "active"
        active.mkdir(parents=True)
        (skills_dir / "candidates").mkdir()
        index = []
        for item in skills:
            document_dir = active / item["name"]
            document_dir.mkdir()
            (document_dir / SKILL_FILE).write_text(item["content"], encoding="utf-8", newline="\n")
            index.append({key: item[key] for key in ("name", "description", "version")})
        (active / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except Exception:
        shutil.rmtree(root, ignore_errors=True)
        raise


if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    _kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    _open_windows = _kernel.CreateFileW
    _open_windows.argtypes = (
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    )
    _open_windows.restype = wintypes.HANDLE
    _close_windows = _kernel.CloseHandle
    _close_windows.argtypes = (wintypes.HANDLE,)
    _close_windows.restype = wintypes.BOOL

    @contextmanager
    def _windows_entry(path: Path):
        # Open the entry itself and keep it pinned against writes and replacement
        # while inspecting it or reading a child by path.
        read_attributes, share_read, open_existing = 0x80, 1, 3
        open_reparse_point, backup_semantics = 0x00200000, 0x02000000
        handle = _open_windows(
            str(path), read_attributes, share_read, None, open_existing,
            open_reparse_point | backup_semantics, None,
        )
        if handle == wintypes.HANDLE(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            metadata = path.lstat()
            if metadata.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                raise ValueError("Skill workspace cannot contain symbolic links or reparse points")
            yield metadata
        finally:
            _close_windows(handle)


@contextmanager
def _directory(parent: int | Path, name: str | None = None):
    if os.name == "nt":
        path = parent if name is None else parent / name
        with _windows_entry(path) as metadata:
            if not stat.S_ISDIR(metadata.st_mode):
                raise ValueError("Skill workspace path is not a directory")
            yield path
        return

    if name is None:
        descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    else:
        descriptor = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    try:
        yield descriptor
    finally:
        os.close(descriptor)


def _read_document(parent: int | Path, limit: int) -> str:
    if os.name == "nt":
        path = parent / SKILL_FILE
        with _windows_entry(path) as metadata:
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise ValueError("Skill draft must be a regular file with one link")
            with path.open("rb") as document:
                data = document.read(limit + 1)
    else:
        expected = os.stat(SKILL_FILE, dir_fd=parent, follow_symlinks=False)
        if not stat.S_ISREG(expected.st_mode) or expected.st_nlink != 1:
            raise ValueError("Skill draft must be a regular file with one link")
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        descriptor = os.open(SKILL_FILE, flags, dir_fd=parent)
        with os.fdopen(descriptor, "rb") as document:
            metadata = os.fstat(document.fileno())
            if not os.path.samestat(expected, metadata) or metadata.st_nlink != 1:
                raise ValueError("Skill draft changed while being opened")
            data = document.read(limit + 1)
    if len(data) > limit:
        raise ValueError("Skill draft exceeds its size limit")
    return data.decode("utf-8")


def collect_skill_candidates(workspace: Path) -> dict:
    candidates = []
    rejected = 0
    remaining = MAX_WORKSPACE_SKILL_BYTES
    try:
        with (
            _directory(workspace) as run_dir,
            _directory(run_dir, ROOT) as root,
            _directory(root, "skills") as skills_dir,
            _directory(skills_dir, "candidates") as incoming,
            os.scandir(incoming) as entries,
        ):
            for entry_number, entry in enumerate(islice(entries, MAX_WORKSPACE_SKILLS + 1)):
                if entry_number == MAX_WORKSPACE_SKILLS:
                    rejected += 1
                    break
                name = entry.name
                if not _valid_name(name) or len(name) >= remaining:
                    rejected += 1
                    continue
                try:
                    with _directory(incoming, name) as parent:
                        content = _read_document(parent, min(MAX_SKILL_BYTES, remaining - len(name)))
                except (OSError, ValueError):
                    rejected += 1
                    continue
                candidates.append({"name": name, "content": content})
                remaining -= len(content.encode("utf-8")) + len(name)
    except (OSError, ValueError):
        rejected += 1
    return {"candidates": candidates, "rejected": rejected}
