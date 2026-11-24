"""Connect an execution backend to a Harness server over an outbound WebSocket."""

import asyncio
import base64
import inspect
import ipaddress
import logging
import secrets
from contextlib import suppress
from typing import Any
from urllib.parse import urlsplit

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, WebSocketException

from harness.execution.protocol import (
    ARTIFACT_CHUNK_BYTES,
    HANDSHAKE_TIMEOUT,
    IMAGE_TYPES,
    MAX_CALLS_PER_RUN,
    MAX_IMAGE_BYTES,
    MAX_JSON_BYTES,
    VERSION,
    ExecutionBackend,
    authentication_proof,
    decode,
    encode,
    is_identifier,
    is_nonce,
    validate_artifact_response,
    verify_proof,
)

_LOG = logging.getLogger(__name__)
_MAX_PENDING_REQUESTS = 8


def _validate_url(url: str, allow_insecure: bool) -> None:
    parts = urlsplit(url)
    if (
        parts.scheme not in {"ws", "wss"} or not parts.hostname
        or parts.username is not None or parts.password is not None or parts.fragment
    ):
        raise ValueError("Expected a ws:// or wss:// worker endpoint without user information")
    if parts.scheme == "wss" or allow_insecure:
        return
    if parts.hostname == "localhost":
        return
    try:
        if ipaddress.ip_address(parts.hostname).is_loopback:
            return
    except ValueError:
        pass
    raise ValueError("A non-loopback worker endpoint requires wss://")


class _Runtime:
    def __init__(self, backend: ExecutionBackend):
        self.backend = backend
        self.lock = asyncio.Lock()
        self.active_run: dict[str, str] | None = None
        self.call_ids: set[str] = set()
        self.pending: set[asyncio.Task[None]] = set()

    async def execute(
        self, call_id: str, method: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        run_id = params.get("run_id")
        if not is_identifier(run_id):
            raise ValueError("Invalid run ID")
        async with self.lock:
            if method in {"run.open", "run.resume"}:
                workspace_id = params.get("workspace_id")
                if not is_identifier(workspace_id):
                    raise ValueError("Invalid workspace ID")
                requested_run = {"run_id": run_id, "workspace_id": workspace_id}
                if self.active_run == requested_run:
                    return {}
                if method == "run.resume":
                    raise ValueError("The original workspace is unavailable. Start a new run.")
                if self.active_run is not None:
                    raise ValueError("Worker already has an active run")
                await self.backend.open_run(run_id, workspace_id)
                self.active_run = requested_run
                self.call_ids.clear()
                return {}
            if method == "run.close" and self.active_run is None:
                return {}
            if self.active_run is None or self.active_run["run_id"] != run_id:
                raise ValueError("Run is not active on this worker")
            if method == "run.close":
                await self.backend.close_run(run_id)
                self.active_run = None
                self.call_ids.clear()
                return {}
            if method == "skills.prepare":
                prepare = getattr(self.backend, "prepare_skill_workspace", None)
                skills = params.get("skills")
                if prepare is None or not isinstance(skills, list):
                    raise ValueError("Worker skill workspace or snapshot is unavailable")
                await prepare(run_id, skills)
                return {}
            if method == "skills.collect":
                collect = getattr(self.backend, "collect_skill_candidates", None)
                if collect is None:
                    raise ValueError("Worker skill workspace is unavailable")
                return await collect(run_id)
            if method == "artifacts.stage":
                stage = getattr(self.backend, "stage_artifact", None)
                path, encoded = params.get("path"), params.get("data")
                if (
                    stage is None or not isinstance(path, str) or len(path) > 1024
                    or not isinstance(encoded, str) or len(encoded) > 4 * ((ARTIFACT_CHUNK_BYTES + 2) // 3)
                ):
                    raise ValueError("Worker artifact workspace or upload chunk is invalid")
                data = base64.b64decode(encoded, validate=True)
                await stage(run_id, path, params.get("offset"), params.get("total"), data)
                return {}
            if call_id in self.call_ids:
                raise ValueError("Call ID already handled. Check the run trace for its outcome.")
            if len(self.call_ids) >= MAX_CALLS_PER_RUN:
                raise ValueError("Run exceeded the worker call limit")
            if method == "bash.execute":
                command = params.get("command")
                timeout = params.get("timeout")
                if not isinstance(command, str) or not command.strip():
                    raise ValueError("Bash requires a command")
                if type(timeout) is not int or not 1 <= timeout <= 3600:
                    raise ValueError("Bash timeout must be between 1 and 3600 seconds")
                self.call_ids.add(call_id)
                return await self.backend.bash(run_id, command, timeout)
            if method in {"computer.execute", "computer.cli"}:
                action = params.get("action")
                if not isinstance(action, dict):
                    raise ValueError("Computer action must be an object")
                if method == "computer.cli":
                    execute = getattr(self.backend, "computer_cli", None)
                    observe, output = params.get("observe"), params.get("output")
                    if execute is None or type(observe) is not bool or (
                        output is not None and (not isinstance(output, str) or len(output) > 512)
                    ):
                        raise ValueError("Invalid computer CLI options")
                    self.call_ids.add(call_id)
                    return await execute(run_id, action, observe=observe, output=output)
                self.call_ids.add(call_id)
                return await self.backend.computer(run_id, action)
            raise ValueError("Unknown worker method")

    async def respond(
        self,
        websocket: ClientConnection,
        send_lock: asyncio.Lock,
        call_id: str,
        method: str,
        params: dict[str, Any],
    ) -> None:
        image = None
        payloads = []
        try:
            result = dict(await self.execute(call_id, method, params))
            image = result.pop("image", None)
            if image is not None:
                if (
                    not isinstance(image, bytes) or not 0 < len(image) <= MAX_IMAGE_BYTES
                    or result.get("mime_type") not in IMAGE_TYPES
                ):
                    raise ValueError("Worker screenshot exceeds the limit or has an unsupported format")
                result["image_bytes"] = len(image)
            if "content" in result:
                blocks = []
                for original in result["content"]:
                    block = dict(original)
                    if block.get("type") != "text":
                        data = block.pop("data", None)
                        if not isinstance(data, bytes):
                            raise ValueError("Worker artifact is missing file data")
                        block["data_bytes"] = len(data)
                        payloads.append(data)
                    blocks.append(block)
                validate_artifact_response(blocks)
                result["content"] = blocks
            response = encode({"id": call_id, "result": result})
        except Exception as exc:
            image = None
            payloads.clear()
            response = encode({"id": call_id, "error": str(exc)[:1000] or type(exc).__name__})
        async with send_lock:
            with suppress(ConnectionClosed):
                await websocket.send(response)
                if image is not None:
                    await websocket.send(image)
                for data in payloads:
                    for offset in range(0, len(data), ARTIFACT_CHUNK_BYTES):
                        await websocket.send(data[offset:offset + ARTIFACT_CHUNK_BYTES])

    async def close(self) -> None:
        tasks = tuple(self.pending)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        close_backend = getattr(self.backend, "close", None)
        if close_backend is not None:
            result = close_backend()
            if inspect.isawaitable(result):
                await result
        elif self.active_run is not None:
            await self.backend.close_run(self.active_run["run_id"])


async def _authenticate(
    websocket: ClientConnection, runtime: _Runtime, worker_id: str, secret: str
) -> float:
    challenge = decode(await websocket.recv())
    server_nonce = challenge.get("server_nonce")
    interval = challenge.get("heartbeat_interval")
    if (
        challenge.get("event") != "challenge" or challenge.get("version") != VERSION
        or not is_nonce(server_nonce)
        or type(interval) not in (int, float) or not 0.25 <= interval <= 60
    ):
        raise ValueError("Invalid Harness server challenge")
    capabilities = runtime.backend.capabilities()
    if inspect.isawaitable(capabilities):
        capabilities = await capabilities
    identity = {
        "server_nonce": server_nonce,
        "client_nonce": secrets.token_hex(32),
        "worker_id": worker_id,
        "capabilities": capabilities,
        "active_run": runtime.active_run,
        "heartbeat_interval": interval,
    }
    await websocket.send(encode({
        "event": "hello",
        "version": VERSION,
        "client_nonce": identity["client_nonce"],
        "worker_id": worker_id,
        "capabilities": capabilities,
        "active_run": identity["active_run"],
        "proof": authentication_proof(secret, identity, "worker"),
    }))
    ready = decode(await websocket.recv())
    if ready.get("event") != "ready" or not verify_proof(
        secret, identity, "server", ready.get("proof")
    ):
        raise ValueError("Harness server authentication failed")
    return interval


async def _heartbeat(
    websocket: ClientConnection, send_lock: asyncio.Lock, interval: float
) -> None:
    message = encode({"event": "heartbeat"})
    with suppress(ConnectionClosed):
        while True:
            await asyncio.sleep(interval)
            async with send_lock:
                await websocket.send(message)


async def _client_session(
    websocket: ClientConnection, runtime: _Runtime, interval: float
) -> None:
    send_lock = asyncio.Lock()
    heartbeat = asyncio.create_task(_heartbeat(websocket, send_lock, interval))
    try:
        async for frame in websocket:
            request = decode(frame)
            call_id = request.get("id")
            method = request.get("method")
            params = request.get("params")
            if not is_identifier(call_id) or not isinstance(method, str) or not isinstance(params, dict):
                raise ValueError("Invalid call from Harness server")
            if len(runtime.pending) >= _MAX_PENDING_REQUESTS:
                async with send_lock:
                    await websocket.send(encode({"id": call_id, "error": "Worker is busy"}))
                continue
            task = asyncio.create_task(runtime.respond(websocket, send_lock, call_id, method, params))
            runtime.pending.add(task)
            task.add_done_callback(runtime.pending.discard)
    finally:
        heartbeat.cancel()
        await asyncio.gather(heartbeat, return_exceptions=True)


async def serve_worker(
    backend: ExecutionBackend,
    *,
    url: str,
    worker_id: str,
    secret: str,
    reconnect: bool = True,
    allow_insecure: bool = False,
) -> None:
    """Serve a worker and keep its current run available across transient disconnects."""
    _validate_url(url, allow_insecure)
    if not is_identifier(worker_id) or not isinstance(secret, str) or not secret:
        raise ValueError("A valid worker ID and nonempty worker secret are required")
    runtime = _Runtime(backend)
    delay = 1.0
    try:
        while True:
            try:
                async with connect(
                    url, open_timeout=HANDSHAKE_TIMEOUT, close_timeout=3, max_size=MAX_JSON_BYTES
                ) as websocket:
                    async with asyncio.timeout(HANDSHAKE_TIMEOUT):
                        interval = await _authenticate(websocket, runtime, worker_id, secret)
                    delay = 1.0
                    _LOG.info("Worker %s connected", worker_id)
                    await _client_session(websocket, runtime, interval)
            except (OSError, TimeoutError, ValueError, WebSocketException) as exc:
                if not reconnect:
                    raise
                _LOG.warning("Worker %s lost its connection (%s); retrying", worker_id, type(exc).__name__)
            if not reconnect:
                return
            await asyncio.sleep(delay)
            delay = min(delay * 2, 15.0)
    finally:
        await runtime.close()
