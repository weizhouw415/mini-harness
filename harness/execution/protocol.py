"""Wire constants and the interface shared by the execution backends."""

import hashlib
import hmac
import json
import re
from typing import Any, Protocol

VERSION = 1
HANDSHAKE_TIMEOUT = 10.0
MAX_JSON_BYTES = 1024 * 1024
MAX_IMAGE_BYTES = 8 * 1024 * 1024
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


def authentication_proof(secret: str, identity: dict[str, Any], role: str) -> str:
    transcript = encode({"version": VERSION, "role": role, "identity": identity})
    return hmac.new(secret.encode(), transcript.encode(), hashlib.sha256).hexdigest()


def verify_proof(secret: str, identity: dict[str, Any], role: str, provided: Any) -> bool:
    return is_nonce(provided) and hmac.compare_digest(
        authentication_proof(secret, identity, role), provided
    )
