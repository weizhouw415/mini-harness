"""Handle run-scoped file uploads and explicit Bash artifact requests."""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
import posixpath
import secrets
import stat
from contextlib import ExitStack, contextmanager
from io import BytesIO
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory

from harness.artifacts.files import file_mime, workspace_path
from harness.execution.protocol import (
    ARTIFACT_CHUNK_BYTES,
    MAX_ARTIFACT_BLOCKS,
    MAX_ARTIFACT_FILE_BYTES,
    MAX_ARTIFACT_IMAGES,
    MAX_ARTIFACT_RESULT_BYTES,
    MAX_ARTIFACT_TEXT_CHARS,
    MAX_IMAGE_BYTES,
)
from workers.common.skill_workspace import _directory

if os.name == "nt":
    from workers.common.skill_workspace import _windows_entry


MAX_REQUEST_BYTES = 32 * 1024
MAX_REQUEST_FILES = 16
MAX_VIEW_SOURCE_BYTES = 64 * 1024 * 1024
MAX_TEXT_WIRE_BYTES = 128 * 1024
CLI_PREFIX = ".mini-harness-cli-"
_SCREENSHOT_FORMATS = {
    ".png": ("image/png", "PNG"),
    ".jpg": ("image/jpeg", "JPEG"),
    ".jpeg": ("image/jpeg", "JPEG"),
    ".webp": ("image/webp", "WEBP"),
}
_CLI = r"""#!/bin/bash
if [[ "${1-}" == --help || "${2-}" == --help ]]; then
    case "${1-}" in
        view)
            printf '%s\n' 'Usage: harness view [OPTIONS] FILE ...' \
                '  --detail low|auto|high        Set raster preview resolution.' \
                '  --crop X,Y,WIDTH,HEIGHT       Crop a raster image in pixels.' \
                '  --pages 1-3,5                Select PDF, Word, or PowerPoint pages.' \
                '  --overview                   Request a compact overview.' \
                '  --sheet NAME                 Select an XLSX worksheet.' \
                '  --frames N                   Limit image animation or video frames.' \
                '  --start SECONDS --end SECONDS --fps RATE' \
                '                               Select a video time range or frame rate.'
            ;;
        register)
            printf '%s\n' 'Usage: harness register FILE ...' \
                'Make explicitly selected workspace originals downloadable to the operator.'
            ;;
        computer) command -p cat -- "${MINI_HARNESS_ARTIFACT_REQUESTS%/*}/computer-help.txt" ;;
        *) printf '%s\n' 'Usage: harness view [OPTIONS] FILE ... | harness register FILE ... | harness computer ...' ;;
    esac
    exit 0
fi
case "${1-}" in
    view|register) ;;
    computer)
        printf '%s\n' 'Submit harness computer as its own tool command for GUI approval.' >&2
        exit 2
        ;;
    *) printf '%s\n' 'Usage: harness view [OPTIONS] FILE ... | harness register FILE ... | harness computer ...' >&2; exit 2 ;;
esac
if (( $# < 2 || $# > 40 )); then
    printf '%s\n' 'Supply one or more workspace files to harness view or register.' >&2
    exit 2
fi
printf '%s\0' "$#" "$MINI_HARNESS_RUN_ROOT" "$PWD" "$@" >> "$MINI_HARNESS_ARTIFACT_REQUESTS"
"""


@contextmanager
def _workspace_parent(workspace: Path, parts: tuple[str, ...], *, create: bool = False):
    with ExitStack() as stack:
        parent = stack.enter_context(_directory(workspace))
        directory = workspace
        for name in parts[:-1]:
            directory = directory / name
            if create:
                try:
                    if os.name == "nt":
                        directory.mkdir(mode=0o700)
                    else:
                        os.mkdir(name, mode=0o700, dir_fd=parent)
                except FileExistsError:
                    pass
            parent = stack.enter_context(_directory(parent, name))
        yield parent, directory


@contextmanager
def _workspace_file(workspace: Path, parts: tuple[str, ...]):
    with _workspace_parent(workspace, parts) as (parent, directory), ExitStack() as stack:
        name = parts[-1]
        if os.name == "nt":
            path = directory / name
            expected = stack.enter_context(_windows_entry(path))
            if not stat.S_ISREG(expected.st_mode) or expected.st_nlink != 1:
                raise ValueError("Artifact must be a regular workspace file with one link.")
            file = stack.enter_context(path.open("rb"))
        else:
            expected = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISREG(expected.st_mode) or expected.st_nlink != 1:
                raise ValueError("Artifact must be a regular workspace file with one link.")
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            descriptor = os.open(name, flags, dir_fd=parent)
            file = stack.enter_context(os.fdopen(descriptor, "rb"))
        actual = os.fstat(file.fileno())
        if not os.path.samestat(expected, actual) or actual.st_nlink != 1:
            raise ValueError("Artifact changed before it could be opened.")
        yield file, actual.st_size


def stage_upload(workspace: Path, path: str, offset: int, total: int, data: bytes) -> None:
    parts = tuple(workspace_path(path).split("/"))
    if not (
        2 <= len(parts) <= 8 and parts[0] == "uploads"
        and not parts[-1].casefold().startswith(".upload-")
        and type(offset) is int and type(total) is int
        and 0 <= offset <= offset + len(data) <= total <= MAX_ARTIFACT_FILE_BYTES
        and len(data) <= ARTIFACT_CHUNK_BYTES and (data or total == 0)
    ):
        raise ValueError("Invalid workspace upload chunk.")
    partial = ".upload-" + hashlib.sha256(path.encode()).hexdigest()
    with _workspace_parent(workspace, parts, create=True) as (parent, directory):
        destination = parts[-1]
        if os.name == "nt":
            path = directory / partial
            if offset == 0:
                path.unlink(missing_ok=True)
                file = path.open("xb")
            else:
                metadata = path.lstat()
                if metadata.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT or metadata.st_nlink != 1:
                    raise ValueError("Invalid workspace upload temporary file.")
                file = path.open("r+b")
        else:
            if offset == 0:
                try:
                    os.unlink(partial, dir_fd=parent)
                except FileNotFoundError:
                    pass
            flags = os.O_WRONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            if offset == 0:
                flags |= os.O_CREAT | os.O_EXCL
            descriptor = os.open(partial, flags, mode=0o600, dir_fd=parent)
            file = os.fdopen(descriptor, "wb")
        with file:
            metadata = os.fstat(file.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or metadata.st_size != offset:
                raise ValueError("Workspace upload offset does not match the staged file.")
            file.seek(offset)
            file.write(data)
        if offset + len(data) == total:
            if os.name == "nt":
                path.replace(directory / destination)
            else:
                os.replace(partial, destination, src_dir_fd=parent, dst_dir_fd=parent)


def screenshot_output_path(path: str) -> tuple[str, ...]:
    parts = tuple(workspace_path(path).split("/"))
    first = parts[0].casefold()
    if first == ".mini-harness" or first.startswith(CLI_PREFIX):
        raise ValueError("Screenshot output must be in the run workspace's user files.")
    if PurePosixPath(path).suffix.lower() not in _SCREENSHOT_FORMATS:
        raise ValueError("Screenshot output requires a PNG, JPEG, or WebP file name.")
    return parts


def save_screenshot(workspace: Path, parts: tuple[str, ...], data: bytes, mime_type: str) -> dict:
    if not isinstance(data, bytes) or not 0 < len(data) <= MAX_IMAGE_BYTES:
        raise ValueError("Screenshot data is unavailable or too large.")
    destination = parts[-1]
    rendered_mime, format_name = _SCREENSHOT_FORMATS[PurePosixPath(destination).suffix.lower()]
    if mime_type != rendered_mime:
        from PIL import Image

        with Image.open(BytesIO(data)) as screenshot, BytesIO() as encoded:
            image = screenshot.convert("RGB") if format_name == "JPEG" else screenshot
            image.save(encoded, format_name)
            if encoded.tell() > MAX_ARTIFACT_FILE_BYTES:
                raise ValueError("Screenshot output is too large.")
            data = encoded.getvalue()
    temporary = ".screenshot-" + secrets.token_hex(16)
    with _workspace_parent(workspace, parts, create=True) as (parent, directory):
        if os.name == "nt":
            target = directory / destination
            partial = directory / temporary
            try:
                metadata = target.lstat()
            except FileNotFoundError:
                metadata = None
            if metadata is not None and metadata.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                raise ValueError("Screenshot output cannot replace a reparse point.")
        else:
            try:
                metadata = os.stat(destination, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                metadata = None
        if metadata is not None and (not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1):
            raise ValueError("Screenshot output must be a regular workspace file with one link.")
        if os.name == "nt":
            file = partial.open("xb")
        else:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
            descriptor = os.open(temporary, flags, mode=0o600, dir_fd=parent)
            file = os.fdopen(descriptor, "wb")
        try:
            with file:
                file.write(data)
            if os.name == "nt":
                partial.replace(target)
            else:
                os.replace(temporary, destination, src_dir_fd=parent, dst_dir_fd=parent)
        finally:
            try:
                if os.name == "nt":
                    partial.unlink()
                else:
                    os.unlink(temporary, dir_fd=parent)
            except FileNotFoundError:
                pass
    return {"path": "/".join(parts), "size": len(data), "output_mime_type": rendered_mime}


class _Arguments(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def _crop(value: str) -> tuple[int, int, int, int]:
    try:
        x, y, width, height = map(int, value.split(","))
    except ValueError as error:
        raise ValueError("--crop requires X,Y,WIDTH,HEIGHT.") from error
    return x, y, width, height


def _parse(command: str, args: list[str]):
    parser = _Arguments(prog=f"harness {command}", add_help=False, allow_abbrev=False)
    parser.add_argument("paths", metavar="FILE", nargs="+")
    if command == "view":
        parser.add_argument("--detail", choices=("low", "auto", "high"), default="auto")
        parser.add_argument("--crop", type=_crop)
        parser.add_argument("--pages")
        parser.add_argument("--overview", action="store_true")
        parser.add_argument("--sheet")
        parser.add_argument("--frames", type=int)
        parser.add_argument("--start", type=float)
        parser.add_argument("--end", type=float)
        parser.add_argument("--fps", type=float)
    options = vars(parser.parse_intermixed_args(args))
    return options.pop("paths"), options


def _relative_path(root: str, cwd: str, path: str) -> tuple[str, ...]:
    if not root.startswith("/") or not cwd.startswith("/") or len(path) > 1024 or not path:
        raise ValueError("Artifact path must point to a file inside the run workspace.")
    resolved = PurePosixPath(posixpath.normpath(posixpath.join(cwd, path)))
    if not resolved.is_relative_to(root):
        raise ValueError("Artifact path must point to a file inside the run workspace.")
    relative = resolved.relative_to(root)
    parts = relative.parts
    first = parts[0].casefold() if parts else ""
    if not first or first == ".mini-harness" or first.startswith(CLI_PREFIX):
        raise ValueError("Artifact path must point to a user file inside the run workspace.")
    workspace_path(relative.as_posix())
    return parts


@contextmanager
def artifact_requests(workspace: Path):
    from harness.execution.computer_cli import COMPUTER_HELP

    with TemporaryDirectory(prefix=CLI_PREFIX, dir=workspace) as directory:
        channel = Path(directory)
        script = channel / "harness"
        script.write_text(_CLI, encoding="utf-8", newline="\n")
        script.chmod(0o700)
        (channel / "computer-help.txt").write_text(COMPUTER_HELP.rstrip() + "\n", encoding="utf-8", newline="\n")
        (channel / "requests").touch(mode=0o600)
        setup = (
            f'export PATH="$PWD/{channel.name}:$PATH"\n'
            'export MINI_HARNESS_RUN_ROOT="$PWD"\n'
            f'export MINI_HARNESS_ARTIFACT_REQUESTS="$PWD/{channel.name}/requests"\n'
        )
        yield setup, channel


def _requests(workspace: Path, channel: Path):
    with _workspace_file(workspace, (channel.name, "requests")) as (stream, _):
        data = stream.read(MAX_REQUEST_BYTES + 1)
    if len(data) > MAX_REQUEST_BYTES:
        raise ValueError("Bash artifact requests exceed the size limit.")
    if not data:
        return
    fields = data.decode().split("\0")
    if fields.pop():
        raise ValueError("Incomplete Bash artifact request.")
    index = 0
    while index < len(fields):
        count = int(fields[index])
        end = index + 3 + count
        if not 2 <= count <= 40 or end > len(fields):
            raise ValueError("Invalid Bash artifact request.")
        root, cwd, command, *args = fields[index + 1:end]
        if command not in {"view", "register"}:
            raise ValueError("Unknown Bash artifact request.")
        yield root, cwd, command, args
        index = end


def _file_blocks(workspace: Path, parts: tuple[str, ...], command: str, options: dict,
                 remaining_bytes: int, remaining_images: int):
    name = parts[-1]
    with _workspace_file(workspace, parts) as (source, size):
        if command == "register":
            limit = min(MAX_ARTIFACT_FILE_BYTES, remaining_bytes)
            if size > limit:
                raise ValueError("File exceeds the current artifact export size limit.")
            data = source.read(limit + 1)
            if len(data) > limit:
                raise ValueError("File exceeds the current artifact export size limit.")
            return [{
                "type": "artifact", "path": "/".join(parts), "name": name,
                "mime_type": file_mime(name), "data": data,
            }]
        if size > MAX_VIEW_SOURCE_BYTES:
            raise ValueError("File exceeds the current preview source size limit.")
        from harness.artifacts.preview import preview

        with TemporaryDirectory(prefix="mini-harness-view-") as directory:
            snapshot = Path(directory) / name
            with snapshot.open("xb") as output:
                remaining = MAX_VIEW_SOURCE_BYTES + 1
                while remaining and (chunk := source.read(min(remaining, ARTIFACT_CHUNK_BYTES))):
                    output.write(chunk)
                    remaining -= len(chunk)
            if not remaining:
                raise ValueError("File exceeds the current preview source size limit.")
            return preview(snapshot, max_images=remaining_images, **options)


def collect_artifacts(workspace: Path, channel: Path) -> list[dict]:
    blocks = []
    remaining_bytes, remaining_images = MAX_ARTIFACT_RESULT_BYTES, MAX_ARTIFACT_IMAGES
    remaining_text = MAX_TEXT_WIRE_BYTES
    files = 0
    try:
        for root, cwd, command, args in _requests(workspace, channel):
            paths, options = _parse(command, args)
            for path in paths:
                files += 1
                if files > MAX_REQUEST_FILES:
                    raise ValueError("Too many files requested in one Bash call.")
                try:
                    parts = _relative_path(root, cwd, path)
                    for block in _file_blocks(workspace, parts, command, options, remaining_bytes, remaining_images):
                        if len(blocks) >= MAX_ARTIFACT_BLOCKS - 1:
                            raise ValueError("Bash artifact content limit reached.")
                        if block["type"] == "image":
                            data = base64.b64decode(block.pop("data_base64"), validate=True)
                            if not remaining_images or len(data) > remaining_bytes:
                                raise ValueError("Bash image preview limit reached.")
                            block["data"] = data
                            block["source_path"] = "/".join(parts)
                            remaining_images -= 1
                        if block["type"] == "text":
                            message = block["text"][:MAX_ARTIFACT_TEXT_CHARS]
                            encoded_size = len(json.dumps(message))
                            if encoded_size > remaining_text:
                                raise ValueError("Bash artifact text limit reached.")
                            block["text"] = message
                            remaining_text -= encoded_size
                        else:
                            remaining_bytes -= len(block["data"])
                        blocks.append(block)
                except (OSError, ValueError) as error:
                    if len(blocks) == MAX_ARTIFACT_BLOCKS - 1:
                        raise ValueError("Bash artifact content limit reached.") from error
                    message = str(error) if isinstance(error, ValueError) else "Workspace file could not be opened."
                    blocks.append({"type": "text", "text": f"{path[:400]}: {message[:400]}"})
    except (OSError, ValueError) as error:
        message = str(error) if isinstance(error, ValueError) else "Bash artifact requests could not be opened."
        blocks.append({"type": "text", "text": message[:400]})
    return blocks


async def add_artifact_content(result: dict, workspace: Path, channel: Path | None) -> dict:
    if channel is not None and not result["timed_out"]:
        task = asyncio.create_task(asyncio.to_thread(collect_artifacts, workspace, channel))
        try:
            content = await asyncio.shield(task)
        except asyncio.CancelledError:
            await asyncio.gather(task, return_exceptions=True)
            raise
        if content:
            result["content"] = content
    return result
