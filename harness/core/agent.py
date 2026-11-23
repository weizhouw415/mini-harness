from __future__ import annotations

import asyncio
import json
import logging
from urllib.parse import urlsplit
from uuid import uuid4

from harness.config import Settings
from harness.core.context import TOOLS, build_messages
from harness.core.llm import LLM, ModelError
from harness.execution.router import WorkerCallError, WorkerDisconnected, WorkerManager, WorkerUnavailable
from harness.storage.sqlite import ApprovalMode, RunStore

logger = logging.getLogger(__name__)
COMPUTER_ACTIONS = {name for name in TOOLS[1]["function"]["parameters"]["properties"]["action"]["enum"]}
CLOUD_OBSERVATION_ACTIONS = {"observe", "screenshot"}


class Agent:
    def __init__(self, settings: Settings, store: RunStore, workers: WorkerManager, skills, llm: LLM):
        self.settings, self.store, self.workers, self.skills, self.llm = settings, store, workers, skills, llm
        self.tasks: dict[str, asyncio.Task] = {}

    def schedule(self, run_id: str):
        current = self.tasks.get(run_id)
        if current and not current.done():
            current.add_done_callback(lambda _: self._schedule_queued(run_id))
            return
        task = asyncio.create_task(self._execute(run_id), name=f"run-{run_id}")
        self.tasks[run_id] = task
        task.add_done_callback(lambda _: self.tasks.pop(run_id, None) if self.tasks.get(run_id) is task else None)

    def _schedule_queued(self, run_id: str):
        if self.store.get(run_id)["status"] == "queued":
            self.schedule(run_id)

    async def stop(self):
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _execute(self, run_id: str):
        run = self.store.get(run_id)
        worker_id = run["worker_id"]
        self.store.transition(run_id, {"queued"}, "running")

        def bind(worker):
            nonlocal worker_id
            worker_id = worker.worker_id
            self.store.transition(run_id, {"running"}, "running", worker_id=worker_id,
                                  event=("worker_bound", {"worker_id": worker_id,
                                                          "capabilities": worker.capabilities}))

        try:
            if worker_id:
                worker = await self.workers.resume(worker_id, run_id, run["workspace_id"])
            else:
                worker = await self.workers.acquire(
                    run["requested_worker"], run_id, run["workspace_id"], on_reserved=bind,
                )
            await self._loop(self.store.get(run_id), worker.capabilities)
        except (WorkerUnavailable, WorkerDisconnected) as error:
            worker_id = worker_id or getattr(error, "worker_id", None)
            self.store.transition(run_id, {"running"}, "paused", worker_id=worker_id, error=str(error))
        except asyncio.CancelledError:
            if self.store.get(run_id)["status"] == "running":
                self.store.transition(run_id, {"running"}, "paused", worker_id=worker_id, error="Server stopped.")
            raise
        except (ModelError, WorkerCallError) as error:
            self.store.transition(run_id, {"running"}, "failed", error=str(error))
        except Exception:
            logger.exception("Unexpected error in run %s", run_id)
            if self.store.get(run_id)["status"] == "running":
                self.store.transition(run_id, {"running"}, "failed", error="Unexpected server error. Check server logs.")
        finally:
            if worker_id and self.store.get(run_id)["status"] in {"completed", "failed", "cancelled"}:
                try:
                    await self.workers.release(worker_id, run_id)
                except (WorkerDisconnected, WorkerCallError, WorkerUnavailable):
                    logger.info("Worker cleanup deferred for run %s", run_id)

    async def _loop(self, run: dict, capabilities: dict):
        run_id = run["id"]
        directory = self.store.directory / "artifacts" / run_id
        previous = self.store.previous(run["session_id"], run_id)
        while True:
            events = self.store.events(run_id)
            turns = [event["data"] for event in events if event["type"] == "assistant"]
            if turns:
                latest = turns[-1]
                if not latest["calls"]:
                    self.store.transition(run_id, {"running"}, "completed", output=latest["message"]["content"])
                    return
                if not await self._deliver(run, capabilities, latest["calls"], events):
                    return
                events = self.store.events(run_id)
            if len(turns) >= self.settings.max_steps:
                raise ModelError("Maximum number of model steps reached.")
            messages = build_messages(run, capabilities, self.skills, previous, events, directory,
                                      self.settings.context_chars)
            reply = await self.llm.complete(messages, TOOLS)
            message, calls = self._normalize(reply)
            self.store.event(run_id, "assistant", {"message": message, "calls": calls, "usage": reply.get("_usage")})

    def _normalize(self, reply: dict) -> tuple[dict, list[dict]]:
        if not isinstance(reply, dict):
            raise ModelError("Model returned an invalid assistant message.")
        text = reply.get("content")
        if text is not None and not isinstance(text, str):
            raise ModelError("Model returned invalid assistant content.")
        raw_calls = reply.get("tool_calls") or []
        if not isinstance(raw_calls, list) or len(raw_calls) > 8:
            raise ModelError("Model returned an invalid number of tool calls.")
        message = {"role": "assistant", "content": text}
        calls, ids, tool_calls = [], set(), []
        for item in raw_calls:
            try:
                provider_id, function = item["id"], item["function"]
                name, arguments = function["name"], function["arguments"]
            except (KeyError, TypeError) as error:
                raise ModelError("Model returned a malformed tool call.") from error
            if not all(isinstance(value, str) for value in (provider_id, name, arguments)):
                raise ModelError("Model tool calls require string IDs, names and arguments.")
            if not provider_id or provider_id in ids or len(arguments) > 65_536:
                raise ModelError("Model returned duplicate IDs or oversized tool arguments.")
            ids.add(provider_id)
            tool_calls.append({"id": provider_id, "type": "function", "function": {
                "name": name, "arguments": arguments,
            }})
            calls.append({"call_id": uuid4().hex, "provider_id": provider_id, "name": name, "arguments": arguments})
        if calls:
            message["tool_calls"] = tool_calls
        elif not text:
            raise ModelError("Model returned neither text nor tool calls.")
        return message, calls

    async def _deliver(self, run: dict, capabilities: dict, calls: list[dict], events: list[dict]) -> bool:
        run_id, worker_id = run["id"], run["worker_id"]
        outcomes = {event["data"]["call_id"] for event in events if event["type"] == "tool_result"}
        started = {event["data"]["call_id"] for event in events if event["type"] == "tool_call"}
        approvals = {event["data"]["call_id"]: event["data"]["approved"]
                     for event in events if event["type"] == "approval_decision"}
        for call in calls:
            call_id = call["call_id"]
            if call_id in outcomes:
                continue
            if call_id in started:
                self._result(run_id, call, self._unknown())
                continue
            try:
                args = self._arguments(call)
            except ValueError as error:
                self._result(run_id, call, {"error": str(error)})
                continue
            if approvals.get(call_id) is False:
                self._result(run_id, call, {"error": "The operator declined this action."})
                continue
            if call_id not in approvals and self._needs_approval(
                run["approval_mode"], call["name"], args, capabilities,
            ):
                approval = {"call_id": call_id, "tool": call["name"], "arguments": args}
                self.store.transition(run_id, {"running"}, "awaiting_approval", approval=approval,
                                      event=("approval_requested", approval))
                return False

            self.store.event(run_id, "tool_call", {"call_id": call_id, "tool": call["name"], "arguments": args})
            try:
                if call["name"] == "bash":
                    result = await self.workers.bash(worker_id, run_id, call_id, args["command"], args["timeout"])
                else:
                    result = await self.workers.computer(worker_id, run_id, call_id, args)
            except WorkerDisconnected:
                self._result(run_id, call, self._unknown())
                raise
            except WorkerCallError as error:
                result = {"error": str(error)}
            self._result(run_id, call, result)
        return True

    def _arguments(self, call: dict) -> dict:
        try:
            args = json.loads(call["arguments"])
        except ValueError as error:
            raise ValueError("Tool arguments must be a JSON object.") from error
        if not isinstance(args, dict):
            raise ValueError("Tool arguments must be a JSON object.")
        if call["name"] == "bash":
            command, timeout = args.get("command"), args.get("timeout", 30)
            if set(args) - {"command", "timeout"} or not isinstance(command, str) or not command.strip():
                raise ValueError("bash requires a nonempty command string and optional timeout.")
            if type(timeout) is not int or not 1 <= timeout <= self.settings.max_tool_seconds:
                raise ValueError(f"bash timeout must be 1–{self.settings.max_tool_seconds} seconds.")
            return {"command": command, "timeout": timeout}
        if call["name"] != "computer_use":
            raise ValueError("Unknown tool.")
        schema = TOOLS[1]["function"]["parameters"]["properties"]
        if not isinstance(args.get("action"), str) or args["action"] not in COMPUTER_ACTIONS:
            raise ValueError("Unknown computer action.")
        for key, value in args.items():
            allowed = schema.get(key, {}).get("type")
            if (allowed == "string" and type(value) is str) or (allowed == "integer" and type(value) is int):
                continue
            raise ValueError(f"Invalid computer action argument: {key}.")
        if args["action"] == "navigate":
            url = urlsplit(args.get("url", ""))
            if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password:
                raise ValueError("Navigate requires an HTTP or HTTPS URL without embedded credentials.")
        return args

    def _needs_approval(self, mode: ApprovalMode, tool: str, args: dict, capabilities: dict) -> bool:
        if mode == "allow":
            return False
        if mode == "manual" or capabilities.get("environment") != "cloud-browser":
            return True
        if tool == "bash":
            return not self.settings.trust_cloud_bash
        return mode == "auto" and args["action"] not in CLOUD_OBSERVATION_ACTIONS

    @staticmethod
    def _unknown() -> dict:
        return {"error": "The action outcome is unknown after interruption. Inspect the worker state before retrying.",
                "outcome_unknown": True}

    def _result(self, run_id: str, call: dict, result: dict):
        result = dict(result)
        image = result.pop("image", None)
        mime = result.pop("mime_type", "image/png")
        if image is not None:
            extension = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}.get(mime)
            if not isinstance(image, bytes) or not extension or len(image) > 12 * 1024 * 1024:
                result["error"] = "Invalid or oversized worker screenshot."
            else:
                name = call["call_id"] + extension
                path = self.store.image_path(run_id, name)
                path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                path.write_bytes(image)
                result.update({"artifact": name, "mime_type": mime})
        self.store.event(run_id, "tool_result", {"call_id": call["call_id"], "tool": call["name"], "result": result})
