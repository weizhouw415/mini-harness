from __future__ import annotations

import asyncio
import json
import logging
from urllib.parse import urlsplit
from uuid import uuid4

from harness.artifacts.files import MAX_PREVIEWS
from harness.artifacts.preview import preview
from harness.config import Settings
from harness.core.approval import ApprovalDecision, decide_approval
from harness.core.context import DEFAULT_BASH_TIMEOUT, build_messages, build_tools
from harness.core.llm import LLM, ModelError
from harness.core.multimodal import artifact_block, persist_content
from harness.execution.computer_cli import COMPUTER_HELP, parse_computer_command
from harness.execution.router import WorkerCallError, WorkerDisconnected, WorkerManager, WorkerUnavailable
from harness.memory.commands import is_memory_command, run_memory_command
from harness.memory.working import working_checkpoint
from harness.storage.sqlite import RunStore
from workers.common.actions import GUI_ACTIONS, SCROLL_DIRECTIONS, point_fields, scroll_deltas, text_field

logger = logging.getLogger(__name__)
COMPUTER_ACTIONS = {*GUI_ACTIONS, "observe", "navigate", "press", "download", "upload", "screen_info"}
COMPUTER_TEXT_FIELDS = {"action", "target", "direction", "url", "selector", "text", "key", "path"}
COMPUTER_INTEGER_FIELDS = {"x", "y", "end_x", "end_y", "amount", "dx", "dy"}
ASSISTANT_DELTA_INTERVAL = 0.15


class Agent:
    def __init__(self, settings: Settings, store: RunStore, workers: WorkerManager, llm: LLM, *,
                 capability_config=None, memory_store=None, memory_config=None):
        self.settings, self.store, self.workers, self.llm = settings, store, workers, llm
        self.capability_config = capability_config
        self.memory_store, self.memory_config = memory_store, memory_config
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
            elif run["worker_target"]:
                worker = await self.workers.acquire_target(
                    run["worker_target"], run_id, run["workspace_id"], on_reserved=bind,
                )
            else:
                worker = await self.workers.acquire(
                    run["requested_worker"], run_id, run["workspace_id"], on_reserved=bind,
                )
            await self._loop(self.store.get(run_id), worker.capabilities,
                             getattr(worker, "skill_profiles", ()), getattr(worker, "skill_directory", ""))
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

    async def _loop(self, run: dict, capabilities: dict, skill_profiles=(), skill_directory=""):
        run_id = run["id"]
        snapshots = [event["data"] for event in self.store.events(run_id, include_deltas=False)
                     if event["type"] == "capability_snapshot"]
        enabled = snapshots[0] if snapshots else (dict(self.capability_config.values) if self.capability_config
                                                  else {"view": True, "register": True, "computer_use": True})
        if not snapshots:
            self.store.event(run_id, "capability_snapshot", enabled)
        if not all(enabled.values()) and capabilities.get("cli_capability_toggles") is not True:
            raise WorkerCallError("This Worker cannot enforce CLI capability settings. Update the Worker and retry.")
        directory = self.store.directory / "artifacts" / run_id
        previous = self.store.previous(run["session_id"], run_id)
        memory_ids = None
        await self._prepare_uploads(run, capabilities)
        while True:
            events = self.store.events(run_id, include_deltas=False)
            turns = [event["data"] for event in events if event["type"] == "assistant"]
            if turns:
                latest = turns[-1]
                if not latest["calls"]:
                    output = latest["message"]["content"]
                    self.store.transition(run_id, {"running"}, "completed", output=output,
                                          memory_extract=bool(self.memory_config and self.memory_config.values["enabled"]
                                                              and self.memory_config.values["automatic"]),
                                          skill_review="auto")
                    return
                if not await self._deliver(run, capabilities, latest["calls"], events, enabled):
                    return
                events = self.store.events(run_id, include_deltas=False)
            if len(turns) >= self.settings.max_steps:
                raise ModelError("Maximum number of model steps reached.")
            memories = []
            checkpoint = None
            if self.memory_store and self.memory_config and self.memory_config.values["enabled"]:
                checkpoint = await working_checkpoint(run, events, self.settings.context_chars, self.llm, self.store)
                personal = self.memory_config.values["personal"]
                if memory_ids is None:
                    snapshot = next((event["data"] for event in events if event["type"] == "memory_snapshot"), None)
                    if snapshot is None:
                        matches = self.memory_store.search(run["session_id"], run["task"], limit=5,
                                                           include_global=personal)
                        profile = [item for item in self.memory_store.search(
                            run["session_id"], limit=100, include_global=personal)
                                   if item["kind"] == "preference"][:3]
                        matches = list({item["id"]: item for item in [*profile, *matches]}.values())[:8]
                        snapshot = {"ids": [item["id"] for item in matches]}
                        self.store.event(run_id, "memory_snapshot", snapshot)
                    memory_ids = snapshot["ids"]
                for identifier in memory_ids:
                    try:
                        item = self.memory_store.get(run["session_id"], identifier, include_global=personal)
                    except ValueError:
                        continue
                    remaining = 4000 - sum(len(value["content"]) for value in memories)
                    if remaining <= 0:
                        break
                    memories.append({"id": item["id"], "memory_key": item["memory_key"],
                                     "content": item["content"][:remaining]})
            refresh = getattr(self.workers, "refresh_run_metadata", None)
            if refresh:
                worker = await refresh(run["worker_id"], run_id)
                skill_profiles = worker.skill_profiles
                skill_directory = worker.skill_directory
            messages = build_messages(run, capabilities, skill_profiles, previous, events, directory,
                                      self.settings.context_chars,
                                      file_path=lambda identifier: self.store.run_file(run_id, identifier)[1],
                                      cli_capabilities=enabled, memories=memories,
                                      memory_enabled=bool(self.memory_config and self.memory_config.values["enabled"]),
                                      working_checkpoint=checkpoint, skill_directory=skill_directory)
            await self._complete_turn(run_id, messages, enabled)

    async def _complete_turn(self, run_id: str, messages: list[dict], cli_capabilities: dict | None = None):
        tools = build_tools(cli_capabilities)
        stream_complete = getattr(self.llm, "stream_complete", None)
        generation = {}
        if stream_complete is None:
            reply = await self.llm.complete(messages, tools)
        else:
            generation = {"generation_id": uuid4().hex}
            loop = asyncio.get_running_loop()
            buffered: dict[str, list[str]] = {}
            last_flush = float("-inf")
            scheduled: asyncio.TimerHandle | None = None

            def flush():
                nonlocal last_flush, scheduled
                scheduled = None
                if buffered:
                    delta = {key: "".join(parts) for key, parts in buffered.items()}
                    self.store.event(run_id, "assistant_delta", {**generation, **delta})
                    buffered.clear()
                    last_flush = loop.time()

            def on_delta(delta: dict[str, str]):
                nonlocal scheduled
                for key, text in delta.items():
                    buffered.setdefault(key, []).append(text)
                if scheduled is None:
                    delay = last_flush + ASSISTANT_DELTA_INTERVAL - loop.time()
                    if delay <= 0:
                        flush()
                    else:
                        scheduled = loop.call_later(delay, flush)

            try:
                reply = await stream_complete(messages, tools, on_delta=on_delta)
            finally:
                if scheduled is not None:
                    scheduled.cancel()
                flush()
        if reply.get("_compatibility") == {"reasoning_effort": "none"} and not any(
            event["type"] == "model_compatibility" and event["data"].get("reasoning_effort") == "none"
            for event in self.store.events(run_id, include_deltas=False)
        ):
            self.store.event(run_id, "model_compatibility", {"reasoning_effort": "none"})
        message, calls = self._normalize(reply)
        self.store.event(run_id, "assistant", {**generation, "message": message, "calls": calls,
                                               "usage": reply.get("_usage")})

    async def _prepare_uploads(self, run: dict, capabilities: dict):
        run_id = run["id"]
        uploads = [file for file in self.store.files(run_id) if file["source"] == "upload"]
        if not uploads or any(event["type"] == "uploads_ready"
                              for event in self.store.events(run_id, include_deltas=False)):
            return
        if capabilities.get("artifact_workspace") is not True:
            raise WorkerCallError("This Worker cannot accept attachments. Update the Worker and retry in a new run.")
        content = []
        remaining_images = MAX_PREVIEWS
        remaining_text = min(24_000, self.settings.context_chars // 2)
        for upload in uploads:
            path = self.store.run_file(run_id, upload["id"])[1]
            await self.workers.stage_artifact(run["worker_id"], run_id, upload["path"],
                                              await asyncio.to_thread(path.read_bytes))
            content.append(artifact_block(upload, "artifact"))
            try:
                blocks = await asyncio.to_thread(preview, path, filename=upload["name"], automatic=True,
                                                 max_images=min(3, remaining_images))
            except (OSError, ValueError):
                blocks = [{"type": "text", "text": "Automatic preview is unavailable. The file is staged."}]
            previews = persist_content(self.store, run_id, blocks, preview_prefix=f"previews/{upload['id']}")
            for block in previews:
                if block["type"] == "image":
                    remaining_images -= 1
                    content.append(block)
                elif remaining_text:
                    text = block["text"][:remaining_text]
                    remaining_text -= len(text)
                    content.append({"type": "text", "text": text})
        self.store.event(run_id, "uploads_ready", {"content": content})

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
        reasoning = reply.get("reasoning_content")
        if reasoning is not None:
            if not isinstance(reasoning, str):
                raise ModelError("Model returned invalid reasoning content.")
            message["reasoning_content"] = reasoning
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
            call = {"call_id": uuid4().hex, "provider_id": provider_id, "name": name, "arguments": arguments}
            if name != "bash":
                call["unsupported"] = True
            calls.append(call)
        if calls:
            message["tool_calls"] = tool_calls
        elif not text:
            raise ModelError("Model returned neither text nor tool calls.")
        return message, calls

    async def _deliver(self, run: dict, capabilities: dict, calls: list[dict], events: list[dict],
                       cli_capabilities: dict | None = None) -> bool:
        run_id, worker_id = run["id"], run["worker_id"]
        outcomes = {event["data"]["call_id"] for event in events if event["type"] == "tool_result"}
        started = {event["data"]["call_id"]: event["data"] for event in events if event["type"] == "tool_call"}
        approvals = {event["data"]["call_id"]: event["data"]["approved"]
                     for event in events if event["type"] == "approval_decision"}
        requests = {event["data"]["call_id"]: event["data"]
                    for event in events if event["type"] == "approval_requested"}
        for call in calls:
            call_id = call["call_id"]
            if call_id in outcomes:
                continue
            if call_id in started:
                self._result(run_id, call, self._unknown(), tool=started[call_id]["tool"])
                continue
            previous_request = requests.get(call_id)
            try:
                event = self._dispatch_event(call, previous_request, capabilities, cli_capabilities)
            except ValueError as error:
                self._result(run_id, call, {"error": str(error)})
                continue
            tool, args = event["tool"], event["arguments"]
            if event.get("memory_command"):
                self.store.event(run_id, "tool_call", event)
                result = run_memory_command(args["command"], self.memory_store, self.memory_config, run)
                self._result(run_id, call, result, tool=tool)
                continue
            if approvals.get(call_id) is False:
                self._result(run_id, call, {"error": "The operator declined this action."}, tool=tool)
                continue
            if call_id not in approvals:
                approval_mode = self.store.get(run_id)["approval_mode"]
                decision = decide_approval(
                    approval_mode, tool, args, capabilities,
                    trust_cloud_bash=self.settings.trust_cloud_bash,
                )
                if event.get("help") and approval_mode != "manual":
                    decision = ApprovalDecision(False, "仅查看内置命令帮助。")
                if decision.needs_operator:
                    if decision.reason:
                        event["reason"] = decision.reason
                    request_event = ("approval_requested", event) if event != previous_request else None
                    self.store.transition(run_id, {"running"}, "awaiting_approval", approval=event,
                                          event=request_event)
                    return False
                if decision.reason:
                    event["auto_approval_reason"] = decision.reason
                elif previous_request is not None:
                    event["auto_approval_reason"] = "审批模式已更新，操作直接执行。"

            self.store.event(run_id, "tool_call", event)
            try:
                if event.get("help"):
                    result = {"exit_code": 0, "stdout": COMPUTER_HELP, "stderr": ""}
                elif tool == "bash":
                    if cli_capabilities and not all(cli_capabilities.values()):
                        result = await self.workers.bash(worker_id, run_id, call_id, args["command"], args["timeout"],
                                                         cli_capabilities=cli_capabilities)
                    else:
                        result = await self.workers.bash(worker_id, run_id, call_id, args["command"], args["timeout"])
                elif event.get("entrypoint") == "bash":
                    result = await self._computer_cli(worker_id, run_id, event, capabilities)
                else:
                    result = await self.workers.computer(worker_id, run_id, call_id, args)
            except WorkerDisconnected:
                self._result(run_id, call, self._unknown(), tool=tool)
                raise
            except WorkerCallError as error:
                result = {"error": str(error)}
            self._result(run_id, call, result, tool=tool)
        return True

    def _dispatch_event(self, call: dict, previous_request: dict | None, capabilities: dict,
                        cli_capabilities: dict | None = None) -> dict:
        if call.get("unsupported"):
            raise ValueError("Unknown tool. Use bash.")
        if cli_capabilities is not None and not cli_capabilities.get("computer_use"):
            if call["name"] == "computer_use" or (previous_request and previous_request.get("tool") == "computer_use"):
                raise ValueError("This capability is disabled for this run.")
        if previous_request:
            # Resume with the exact target, arguments, and timeout shown for approval.
            return {key: value for key, value in previous_request.items() if key != "reason"}
        args = self._arguments(call)
        event = {"call_id": call["call_id"], "tool": call["name"], "arguments": args}
        if call["name"] == "computer_use":
            # Compatible with native computer calls persisted before the CLI was introduced.
            event["arguments"] = self._route_computer(args, capabilities)
            return event
        if is_memory_command(args["command"]):
            event["memory_command"] = True
            return event
        command = parse_computer_command(args["command"])
        if command is None:
            return event
        if cli_capabilities is not None and not cli_capabilities.get("computer_use"):
            raise ValueError("This capability is disabled for this run.")
        if command.action is None:
            return {**event, "help": True}
        action = self._route_computer(self._validate_computer(command.action), capabilities)
        if command.observe:
            action["observe"] = True
        if command.output is not None:
            action["output"] = command.output
        return {**event, "tool": "computer_use", "entrypoint": "bash", "arguments": action,
                "command": args["command"], "timeout": args["timeout"]}

    async def _computer_cli(self, worker_id: str, run_id: str, event: dict, capabilities: dict) -> dict:
        action = dict(event["arguments"])
        observe, output = action.pop("observe", False), action.pop("output", None)
        if capabilities.get("computer_cli") is True:
            result = await self.workers.computer_cli(worker_id, run_id, event["call_id"], action,
                                                     observe=observe, output=output, timeout=event["timeout"])
        else:
            if observe or output is not None or action["action"] == "screen_info":
                raise WorkerCallError("This command option requires a Worker with the updated Computer CLI.")
            result = await self.workers.computer(worker_id, run_id, event["call_id"], action)
        output_fields = {key: value for key, value in result.items() if key not in {"image", "content", "error"}}
        return {"exit_code": int(bool(result.get("error"))), "stdout": json.dumps(output_fields, ensure_ascii=False),
                "stderr": result.get("error", ""), **result}

    def _arguments(self, call: dict, previous_request: dict | None = None) -> dict:
        try:
            # Keep the exact arguments already presented to the operator on resume.
            args = previous_request["arguments"] if previous_request else json.loads(call["arguments"])
        except ValueError as error:
            raise ValueError("Tool arguments must be a JSON object.") from error
        if not isinstance(args, dict):
            raise ValueError("Tool arguments must be a JSON object.")
        if call["name"] == "bash":
            command = args.get("command")
            timeout = args.get("timeout", min(DEFAULT_BASH_TIMEOUT, self.settings.max_tool_seconds))
            if set(args) - {"command", "timeout"} or not isinstance(command, str) or not command.strip():
                raise ValueError("bash requires a nonempty command string and optional timeout.")
            if type(timeout) is not int or not 1 <= timeout <= self.settings.max_tool_seconds:
                raise ValueError(f"bash timeout must be 1–{self.settings.max_tool_seconds} seconds.")
            return {"command": command, "timeout": timeout}
        if call["name"] != "computer_use":
            raise ValueError("Unknown tool.")
        return self._validate_computer(args)

    @staticmethod
    def _validate_computer(args: dict) -> dict:
        if not isinstance(args.get("action"), str) or args["action"] not in COMPUTER_ACTIONS:
            raise ValueError("Unknown computer action.")
        for key, value in args.items():
            if key in COMPUTER_TEXT_FIELDS and type(value) is str:
                continue
            if key in COMPUTER_INTEGER_FIELDS and type(value) is int:
                continue
            raise ValueError(f"Invalid computer action argument: {key}.")
        if "target" in args and args["target"] not in {"browser", "desktop"}:
            raise ValueError("Computer target must be browser or desktop.")
        if "direction" in args and args["direction"] not in SCROLL_DIRECTIONS:
            raise ValueError("Scroll direction must be up, down, left, or right.")
        action = args["action"]
        if action == "navigate":
            url = urlsplit(args.get("url", ""))
            if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password:
                raise ValueError("Navigate requires an HTTP or HTTPS URL without embedded credentials.")
        elif action in {"click", "double_click", "right_click", "download"}:
            if "selector" in args:
                text_field(args, "selector")
            else:
                point_fields(args)
        elif action in {"move", "drag"}:
            point_fields(args)
            if action == "drag":
                point_fields(args, "end_x", "end_y")
        elif action in {"key", "press"}:
            text_field(args, "key", limit=80)
        elif action == "type":
            text_field(args, "text", allow_empty=True, limit=100_000)
        elif action == "scroll":
            scroll_deltas(args)
            if "x" in args or "y" in args:
                point_fields(args)
        elif action == "upload":
            text_field(args, "selector")
            text_field(args, "path")
        return args

    @staticmethod
    def _route_computer(args: dict, capabilities: dict) -> dict:
        native_target = "browser" if capabilities.get("environment") == "cloud-browser" else "desktop"
        targets = capabilities.get("computer_targets")
        target = args.get("target", native_target)
        if capabilities.get("computer") is False:
            raise ValueError(f"Computer operations are unavailable on target {target}.")
        if target == "desktop" and args["action"] in {"click", "double_click", "right_click"}:
            point_fields(args)
        if not isinstance(targets, dict):
            if target != native_target:
                raise ValueError(f"This worker does not support computer target: {target}.")
            return args

        actions = targets.get(target)
        if not isinstance(actions, list):
            raise ValueError(f"This worker does not support computer target: {target}.")
        supported_action = "screenshot" if args["action"] == "screen_info" else args["action"]
        if supported_action not in actions:
            raise ValueError(f"Computer action {args['action']} is unavailable on target {target}.")
        return {**args, "target": target}

    @staticmethod
    def _unknown() -> dict:
        return {"error": "The action outcome is unknown after interruption. Inspect the worker state before retrying.",
                "outcome_unknown": True}

    def _result(self, run_id: str, call: dict, result: dict, *, tool: str | None = None):
        result = dict(result)
        blocks = result.pop("content", None)
        content = (persist_content(self.store, run_id, blocks) if isinstance(blocks, list) else [])
        image = result.pop("image", None)
        mime = result.pop("mime_type", "image/png")
        if image is not None:
            extension = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}.get(mime)
            if not isinstance(image, bytes) or not extension or len(image) > 12 * 1024 * 1024:
                result["error"] = "Invalid or oversized worker screenshot."
            else:
                name = call["call_id"] + extension
                file = self.store.add_run_file(run_id, "preview", f"screenshots/{name}", image,
                                               mime_type=mime, legacy_name=name)
                content.append(artifact_block(file, "image"))
                result.update({"artifact": name, "mime_type": mime})
        if content:
            result["content"] = content
        self.store.event(run_id, "tool_result", {"call_id": call["call_id"], "tool": tool or call["name"],
                                                 "result": result})
