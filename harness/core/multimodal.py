from __future__ import annotations

from base64 import b64decode, b64encode
from collections.abc import Callable
from hashlib import sha256
from pathlib import Path

from harness.artifacts.files import MAX_PREVIEWS, RASTER_MIME, workspace_path
from harness.storage.sqlite import NotFound, RunStore


def artifact_block(file: dict, kind: str) -> dict:
    return {
        "type": kind, "artifact_id": file["id"], "name": file["name"], "path": file["path"],
        "mime_type": file["mime_type"], "size": file["size"], "version": file["version"],
    }


def persist_content(store: RunStore, run_id: str, blocks: list, *, preview_prefix: str = "previews") -> list[dict]:
    content = []
    for block in blocks[:32]:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text" and isinstance(block.get("text"), str):
            content.append({"type": "text", "text": block["text"][:8_200]})
        if kind not in ("image", "artifact"):
            continue
        try:
            data = block.get("data")
            if kind == "image" and data is None:
                encoded = block.get("data_base64")
                if isinstance(encoded, str) and len(encoded) <= 1_048_576:
                    data = b64decode(encoded, validate=True)
            if not isinstance(data, bytes):
                raise ValueError("Missing file data.")
            if kind == "image":
                name, mime_type = block.get("name"), block.get("mime_type")
                if (not isinstance(name, str) or "/" in name or len(data) > 768 * 1024
                        or mime_type not in RASTER_MIME):
                    raise ValueError("Invalid or oversized preview image.")
                directory = preview_prefix
                source_path = block.get("source_path")
                if source_path is not None:
                    source_path = workspace_path(source_path)
                    directory += "/" + sha256(source_path.encode()).hexdigest()[:16]
                file = store.add_run_file(run_id, "preview", f"{directory}/{name}", data, mime_type=mime_type)
            else:
                file = store.add_run_file(run_id, "output", block.get("path"), data)
        except ValueError:
            content.append({"type": "text", "text": "A returned file could not be registered."})
        else:
            stored = artifact_block(file, kind)
            if kind == "image" and source_path:
                stored["source_path"] = source_path
            content.append(stored)
    return content


class VisualContext:
    """Encode the current request's selected images for Chat Completions."""

    def __init__(self, file_path: Callable[[str], Path] | None):
        self.file_path = file_path
        self.count = 0
        self.bytes = 0
        self.seen: set[Path] = set()

    def image(self, block: dict, legacy_path: Path | None = None) -> dict | None:
        mime_type = block.get("mime_type")
        if self.count >= MAX_PREVIEWS or mime_type not in RASTER_MIME:
            return None
        try:
            path = legacy_path
            if self.file_path and block.get("artifact_id"):
                path = self.file_path(block["artifact_id"])
            if path is None or path in self.seen:
                return None
            size = path.stat().st_size
            if size > 12 * 1024 * 1024 or (self.count and self.bytes + size > 8 * 1024 * 1024):
                return None
            data = path.read_bytes()
        except (OSError, NotFound):
            return None
        self.count += 1
        self.bytes += len(data)
        self.seen.add(path)
        return {"type": "image_url", "image_url": {
            "url": f"data:{mime_type};base64,{b64encode(data).decode('ascii')}",
        }}
