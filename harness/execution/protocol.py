"""Wire constants and the interface shared by the execution backends."""

import hashlib
import hmac
import json
import re
from typing import Any, Protocol

from harness.artifacts.files import MAX_FILE_BYTES, MAX_PREVIEWS, MAX_UPLOAD_BYTES

VERSION = 1
HANDSHAKE_TIMEOUT = 10.0
MAX_JSON_BYTES = 1024 * 1024
MAX_IMAGE_BYTES = 8 * 1024 * 1024
ARTIFACT_CHUNK_BYTES = 256 * 1024
MAX_ARTIFACT_FILE_BYTES = MAX_FILE_BYTES
MAX_ARTIFACT_RESULT_BYTES = MAX_UPLOAD_BYTES
MAX_ARTIFACT_BLOCKS = 32
MAX_ARTIFACT_IMAGES = MAX_PREVIEWS
MAX_ARTIFACT_TEXT_CHARS = 16_000
MAX_CALLS_PER_RUN = 2048
IMAGE_TYPES = {"image/png", "image/jpeg", "image/webp"}
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_NONCE = re.compile(r"[0-9a-f]{64}\Z")


class ExecutionBackend(Protocol):
    async def open_run(self, run_id: str, workspace_id: str) -> None: ...
    async def bash(self, run_id: str, command: str, timeout: int) -> dict[str, Any]: ...
    async def computer(self, run_id: str, action: dict[str, Any]) -> dict[str, Any]: ...
    async def close_run(self, run_id: str) -> None: ...
    def capabilities(self) -> dict[str, Any]: ...


def is_identifier(value: Any) -> bool:
    return isinstance(value, str) and _IDENTIFIER.fullmatch(value) is not None


def is_nonce(value: Any) -> bool:
    return isinstance(value, str) and _NONCE.fullmatch(value) is not None


def encode(message: dict[str, Any]) -> str:
    encoded = json.dumps(message, separators=(",", ":"), sort_keys=True, allow_nan=False)
    if len(encoded) > MAX_JSON_BYTES:
        raise ValueError("worker message is too large")
    return encoded


def decode(encoded: Any) -> dict[str, Any]:
    if not isinstance(encoded, str) or len(encoded.encode()) > MAX_JSON_BYTES:
        raise ValueError("invalid worker message")
    message = json.loads(encoded)
    if not isinstance(message, dict):
        raise ValueError("expected a worker message object")
    return message


def validate_artifact_response(content: Any) -> None:
    """Validate the typed result metadata before any associated frames are transmitted."""
    if not isinstance(content, list) or len(content) > MAX_ARTIFACT_BLOCKS:
        raise ValueError("Invalid artifact result content")
    total = images = 0
    for block in content:
        if not isinstance(block, dict):
            raise ValueError("Invalid artifact result block")
        kind = block.get("type")
        if kind == "text":
            if (
                set(block) != {"type", "text"} or not isinstance(block["text"], str)
                or len(block["text"]) > MAX_ARTIFACT_TEXT_CHARS
            ):
                raise ValueError("Invalid artifact result text")
            continue
        expected = {"type", "name", "mime_type", "data_bytes"}
        if kind == "artifact":
            expected.add("path")
        elif kind == "image" and "source_path" in block:
            expected.add("source_path")
        if kind not in {"image", "artifact"} or set(block) != expected:
            raise ValueError("Invalid artifact result block")
        size, name, media = block["data_bytes"], block["name"], block["mime_type"]
        if (
            not isinstance(name, str) or not 0 < len(name) <= 255
            or not isinstance(media, str) or not 0 < len(media) <= 100
        ):
            raise ValueError("Invalid artifact file metadata")
        if kind == "image":
            if type(size) is not int or not 0 < size <= MAX_IMAGE_BYTES or media not in IMAGE_TYPES:
                raise ValueError("Invalid artifact preview image")
            if "source_path" in block:
                path = block["source_path"]
                if not isinstance(path, str) or not 0 < len(path) <= 1024:
                    raise ValueError("Invalid artifact preview source path")
            images += 1
        else:
            path = block["path"]
            if (
                type(size) is not int or not 0 <= size <= MAX_ARTIFACT_FILE_BYTES
                or not isinstance(path, str) or not 0 < len(path) <= 1024
            ):
                raise ValueError("Invalid registered artifact")
        total += size
    if total > MAX_ARTIFACT_RESULT_BYTES or images > MAX_ARTIFACT_IMAGES:
        raise ValueError("Artifact result exceeds the transfer limits")


def authentication_proof(secret: str, identity: dict[str, Any], role: str) -> str:
    transcript = encode({"version": VERSION, "role": role, "identity": identity})
    return hmac.new(secret.encode(), transcript.encode(), hashlib.sha256).hexdigest()


def verify_proof(secret: str, identity: dict[str, Any], role: str, provided: Any) -> bool:
    return is_nonce(provided) and hmac.compare_digest(
        authentication_proof(secret, identity, role), provided
    )
