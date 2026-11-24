from __future__ import annotations

import mimetypes
import re
from pathlib import PurePosixPath


MAX_UPLOADS = 8
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_UPLOAD_BYTES = 40 * 1024 * 1024
MAX_PREVIEWS = 8
RASTER_MIME = {"image/png", "image/jpeg", "image/webp"}
_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
_RESERVED = re.compile(r"(?:CON|PRN|AUX|NUL|COM[0-9¹²³]|LPT[0-9¹²³])", re.I)


def file_name(name: str) -> str:
    if not isinstance(name, str) or len(name.encode("utf-8")) > 240:
        raise ValueError("The file name must be at most 240 bytes.")
    if not name.strip() or len(name) > 160 or name.endswith((".", " ")) or _UNSAFE.search(name):
        raise ValueError("Invalid file name.")
    if _RESERVED.fullmatch(name.split(".", 1)[0].rstrip()):
        raise ValueError("This file name is reserved on Windows.")
    return name


def workspace_path(path: str) -> str:
    if not isinstance(path, str) or len(path) > 512 or path.startswith("/"):
        raise ValueError("Invalid workspace path.")
    for part in path.split("/"):
        file_name(part)
    return PurePosixPath(path).as_posix()


def file_mime(name: str) -> str:
    return mimetypes.guess_type(name)[0] or "application/octet-stream"
