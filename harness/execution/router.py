"""Authenticate outbound workers and route a run to a single execution environment."""

import asyncio
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from starlette.websockets import WebSocket, WebSocketDisconnect

from .protocol import (
    HANDSHAKE_TIMEOUT,
    IMAGE_TYPES,
    MAX_IMAGE_BYTES,
    VERSION,
    authentication_proof,
    decode,
    encode,
    is_identifier,
    is_nonce,
    verify_proof,
)


class WorkerUnavailable(RuntimeError):
    """No matching worker can take this run."""


class WorkerDisconnected(WorkerUnavailable):
    """The worker was lost and the outcome of its current operation may be unknown."""

    def __init__(self, worker_id: str, message: str | None = None):
        self.worker_id = worker_id
        super().__init__(message or f"Worker {worker_id} disconnected")


class WorkerCallError(RuntimeError):
    """The worker rejected a call or reported a backend error."""


@dataclass
class WorkerConnection:
    worker_id: str
    capabilities: dict[str, Any]
    websocket: WebSocket
    pending: dict[str, asyncio.Future[dict[str, Any]]] = field(default_factory=dict)
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def request(
        self, call_id: str, method: str, params: dict[str, Any], timeout: float
    ) -> dict[str, Any]:
        if not is_identifier(call_id) or call_id in self.pending:
            raise WorkerCallError("Invalid or already pending worker call ID")
        try:
            message = encode({"id": call_id, "method": method, "params": params})
        except (TypeError, ValueError) as exc:
            raise WorkerCallError("Worker call arguments are invalid or too large") from exc
        future = asyncio.get_running_loop().create_future()
        self.pending[call_id] = future
        try:
            async with asyncio.timeout(timeout):
                async with self.send_lock:
                    await self.websocket.send_text(message)
                return await future
        except TimeoutError as exc:
            raise WorkerDisconnected(
                self.worker_id, f"Worker {self.worker_id} did not confirm {method}"
            ) from exc
        except (WebSocketDisconnect, OSError, RuntimeError) as exc:
            if isinstance(exc, (WorkerDisconnected, WorkerCallError)):
                raise
            raise WorkerDisconnected(self.worker_id) from exc
        finally:
            self.pending.pop(call_id, None)
            if future.done() and not future.cancelled():
                future.exception()
            else:
                future.cancel()

    def disconnect(self) -> None:
        for future in self.pending.values():
            if not future.done():
                future.set_exception(WorkerDisconnected(self.worker_id))


@dataclass
class _Reservation:
    run_id: str
    workspace_id: str
    releasing: bool = False
    closing: bool = False


async def _receive(websocket: WebSocket) -> str | bytes:
    frame = await websocket.receive()
    if frame["type"] == "websocket.disconnect":
        raise WebSocketDisconnect(frame.get("code", 1000))
    if frame.get("text") is not None:
        return frame["text"]
    if frame.get("bytes") is not None:
        return frame["bytes"]
    raise ValueError("Invalid WebSocket frame")


class WorkerManager:
    def __init__(
        self,
        secret: str | Mapping[str, str],
        *,
        heartbeat_timeout: float = 30,
        retain_run: Callable[[str, str, str], bool] | None = None,
    ):
        if heartbeat_timeout < 1:
            raise ValueError("Worker heartbeat timeout must be at least one second")
        if isinstance(secret, str):
            if not secret:
                raise ValueError("A worker secret is required")
        elif any(
            not is_identifier(key) or not isinstance(value, str) or not value
            for key, value in secret.items()
        ):
            raise ValueError("Worker credentials must use valid IDs and nonempty secrets")
        self._secret = secret if isinstance(secret, str) else dict(secret)
        self._heartbeat_timeout = heartbeat_timeout
        self._retain_run = retain_run
        self._connecting: set[str] = set()
        self._workers: dict[str, WorkerConnection] = {}
        self._known_workers: dict[str, dict[str, Any]] = {}
        self._reservations: dict[str, _Reservation] = {}

    def list_workers(self) -> list[dict[str, Any]]:
        workers = []
        for worker_id, capabilities in sorted(self._known_workers.items()):
            reservation = self._reservations.get(worker_id)
            workers.append({
                "worker_id": worker_id,
                "connected": worker_id in self._workers,
                "environment": capabilities.get("environment"),
                "capabilities": capabilities,
                "run_id": reservation.run_id if reservation else None,
                "releasing": reservation.releasing if reservation else False,
            })
        return workers

    async def accept(self, websocket: WebSocket) -> None:
        worker: WorkerConnection | None = None
        cleanup: asyncio.Task[None] | None = None
        close_code = 1000
        await websocket.accept()
        try:
            async with asyncio.timeout(HANDSHAKE_TIMEOUT):
                worker = await self._authenticate(websocket)
            reservation = self._reservations.get(worker.worker_id)
            if reservation and reservation.releasing:
                cleanup = asyncio.create_task(self.release(worker.worker_id, reservation.run_id))
            while True:
                async with asyncio.timeout(self._heartbeat_timeout):
                    message = decode(await _receive(websocket))
                    if message == {"event": "heartbeat"}:
                        continue
                    await self._receive_result(worker, message)
        except WebSocketDisconnect:
            pass
        except (TimeoutError, ValueError, TypeError, OSError, RuntimeError):
            close_code = 1008
        finally:
            if worker:
                if self._workers.get(worker.worker_id) is worker:
                    self._workers.pop(worker.worker_id)
                worker.disconnect()
            if cleanup is not None:
                await asyncio.gather(cleanup, return_exceptions=True)
            try:
                await websocket.close(code=close_code)
            except (RuntimeError, OSError):
                pass

    async def _authenticate(self, websocket: WebSocket) -> WorkerConnection:
        server_nonce = secrets.token_hex(32)
        heartbeat_interval = min(5.0, self._heartbeat_timeout / 3)
        await websocket.send_text(encode({
            "event": "challenge",
            "version": VERSION,
            "server_nonce": server_nonce,
            "heartbeat_interval": heartbeat_interval,
        }))
        hello = decode(await _receive(websocket))
        worker_id = hello.get("worker_id")
        capabilities = hello.get("capabilities")
        active_run = hello.get("active_run")
        if (
            hello.get("event") != "hello"
            or hello.get("version") != VERSION
            or not is_identifier(worker_id)
            or not is_nonce(hello.get("client_nonce"))
            or not isinstance(capabilities, dict)
            or not isinstance(capabilities.get("environment"), str)
            or (
                active_run is not None
                and (
                    not isinstance(active_run, dict)
                    or not is_identifier(active_run.get("run_id"))
                    or not is_identifier(active_run.get("workspace_id"))
                )
            )
        ):
            raise ValueError("Invalid worker hello")
        secret = self._secret if isinstance(self._secret, str) else self._secret.get(worker_id)
        identity = {
            "server_nonce": server_nonce,
            "client_nonce": hello["client_nonce"],
            "worker_id": worker_id,
            "capabilities": capabilities,
            "active_run": active_run,
            "heartbeat_interval": heartbeat_interval,
        }
        if not secret or not verify_proof(secret, identity, "worker", hello.get("proof")):
            raise ValueError("Invalid worker proof")
        if worker_id in self._workers or worker_id in self._connecting:
            raise ValueError("Worker ID is already connected")
        reservation = self._reservations.get(worker_id)
        if active_run is not None and reservation is not None and (
            active_run["run_id"] != reservation.run_id
            or active_run["workspace_id"] != reservation.workspace_id
        ):
            raise ValueError("Worker reports a conflicting active run")
        if active_run is not None and any(
            other_id != worker_id and active_run["run_id"] == item.run_id
            for other_id, item in self._reservations.items()
        ):
            raise ValueError("Worker reports a run reserved on another worker")
        if active_run is not None:
            if reservation is None:
                reservation = _Reservation(active_run["run_id"], active_run["workspace_id"])
            if self._retain_run is not None and not self._retain_run(
                worker_id, reservation.run_id, reservation.workspace_id
            ):
                reservation.releasing = True
            self._reservations[worker_id] = reservation
        worker = WorkerConnection(worker_id, capabilities, websocket)
        self._connecting.add(worker_id)
        try:
            await websocket.send_text(encode({
                "event": "ready",
                "proof": authentication_proof(secret, identity, "server"),
            }))
        finally:
            self._connecting.remove(worker_id)
        self._workers[worker_id] = worker
        self._known_workers[worker_id] = capabilities
        return worker

    async def _receive_result(
        self, worker: WorkerConnection, message: dict[str, Any]
    ) -> None:
        call_id = message.get("id")
        if not is_identifier(call_id):
            raise ValueError("Invalid worker response ID")
        error = message.get("error")
        result = message.get("result")
        if isinstance(error, str) and result is None:
            response: dict[str, Any] | Exception = WorkerCallError(error)
        elif isinstance(result, dict) and error is None and "image" not in result:
            response = result
            if "image_bytes" in result:
                size = result.pop("image_bytes")
                if (
                    type(size) is not int
                    or not 0 < size <= MAX_IMAGE_BYTES
                    or result.get("mime_type") not in IMAGE_TYPES
                ):
                    raise ValueError("Invalid worker image metadata")
                binary = await _receive(worker.websocket)
                if not isinstance(binary, bytes) or len(binary) != size:
                    raise ValueError("Invalid worker image frame")
                result["image"] = binary
        else:
            raise ValueError("Invalid worker response")
        future = worker.pending.get(call_id)
        if future is not None and not future.done():
            if isinstance(response, Exception):
                future.set_exception(response)
            else:
                future.set_result(response)

    async def acquire(
        self,
        worker_id: str | None,
        run_id: str,
        workspace_id: str,
        *,
        on_reserved: Callable[[WorkerConnection], None] | None = None,
    ) -> WorkerConnection:
        self._validate_run(run_id, workspace_id)
        if any(item.run_id == run_id for item in self._reservations.values()):
            raise WorkerUnavailable(f"Run {run_id} is already reserved")
        if worker_id is None:
            worker_id = self._default_worker_id()
        worker = self._workers.get(worker_id)
        if worker is None:
            raise WorkerUnavailable(f"Worker {worker_id} is offline")
        if worker_id in self._reservations:
            raise WorkerUnavailable(f"Worker {worker_id} is already running another task")
        self._reservations[worker_id] = _Reservation(run_id, workspace_id)
        if on_reserved is not None:
            try:
                on_reserved(worker)
            except Exception:
                self._reservations.pop(worker_id, None)
                raise
        try:
            await self._prepare(worker, "run.open", run_id, workspace_id)
        except WorkerCallError:
            self._reservations.pop(worker_id, None)
            raise
        return worker

    def _default_worker_id(self) -> str:
        desktop_ids = sorted(
            worker_id for worker_id, capabilities in self._known_workers.items()
            if capabilities.get("environment") in {"macos-desktop", "windows-desktop"}
            and capabilities.get("computer") is True
        )
        if desktop_ids:
            for worker_id in desktop_ids:
                if worker_id in self._workers and worker_id not in self._reservations:
                    return worker_id
            raise WorkerUnavailable("Desktop workers are busy or offline. Resume when one is available.")

        for worker_id, worker in sorted(self._workers.items()):
            if worker.capabilities["environment"] == "cloud-browser" and worker_id not in self._reservations:
                return worker_id
        raise WorkerUnavailable("No cloud browser worker is available")

    async def resume(
        self, worker_id: str, run_id: str, workspace_id: str
    ) -> WorkerConnection:
        self._validate_run(run_id, workspace_id)
        worker = self._workers.get(worker_id)
        if worker is None:
            raise WorkerDisconnected(worker_id)
        if any(
            key != worker_id and item.run_id == run_id
            for key, item in self._reservations.items()
        ):
            raise WorkerUnavailable(f"Run {run_id} is reserved on a different worker")
        reservation = self._reservations.get(worker_id)
        if reservation is None:
            reservation = _Reservation(run_id, workspace_id)
            self._reservations[worker_id] = reservation
        if (
            reservation.run_id != run_id
            or reservation.workspace_id != workspace_id
            or reservation.releasing
        ):
            raise WorkerUnavailable(f"Worker {worker_id} is reserved for another task")
        await self._prepare(worker, "run.resume", run_id, workspace_id)
        return worker

    async def _prepare(
        self, worker: WorkerConnection, method: str, run_id: str, workspace_id: str
    ) -> None:
        await worker.request(
            secrets.token_hex(16),
            method,
            {"run_id": run_id, "workspace_id": workspace_id},
            60,
        )

    @staticmethod
    def _validate_run(run_id: str, workspace_id: str) -> None:
        if not is_identifier(run_id) or not is_identifier(workspace_id):
            raise ValueError("Invalid run or workspace ID")

    def _get_worker(self, worker_id: str, run_id: str) -> WorkerConnection:
        reservation = self._reservations.get(worker_id)
        if reservation is None or reservation.run_id != run_id or reservation.releasing:
            raise WorkerUnavailable(f"Run {run_id} has no usable reservation on worker {worker_id}")
        worker = self._workers.get(worker_id)
        if worker is None:
            raise WorkerDisconnected(worker_id)
        return worker

    async def bash(
        self, worker_id: str, run_id: str, call_id: str, command: str, timeout: int
    ) -> dict[str, Any]:
        return await self._get_worker(worker_id, run_id).request(
            call_id, "bash.execute",
            {"run_id": run_id, "command": command, "timeout": timeout},
            timeout + 15,
        )

    async def computer(
        self, worker_id: str, run_id: str, call_id: str, action: dict[str, Any]
    ) -> dict[str, Any]:
        return await self._get_worker(worker_id, run_id).request(
            call_id, "computer.execute", {"run_id": run_id, "action": action}, 120
        )

    async def prepare_skill_workspace(self, worker_id: str, run_id: str, skills: list[dict]) -> None:
        await self._get_worker(worker_id, run_id).request(
            secrets.token_hex(16), "skills.prepare", {"run_id": run_id, "skills": skills}, 15,
        )

    async def collect_skill_candidates(self, worker_id: str, run_id: str) -> dict:
        return await self._get_worker(worker_id, run_id).request(
            secrets.token_hex(16), "skills.collect", {"run_id": run_id}, 15,
        )

    async def release(self, worker_id: str, run_id: str) -> None:
        reservation = self._reservations.get(worker_id)
        if reservation is None or reservation.run_id != run_id:
            return
        reservation.releasing = True
        worker = self._workers.get(worker_id)
        if worker is None or reservation.closing:
            return
        reservation.closing = True
        try:
            await worker.request(secrets.token_hex(16), "run.close", {"run_id": run_id}, 30)
        except (WorkerDisconnected, WorkerCallError):
            return
        else:
            if self._reservations.get(worker_id) is reservation:
                self._reservations.pop(worker_id)
        finally:
            reservation.closing = False
