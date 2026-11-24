import json
import shlex
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import httpx

from harness.api.server import create_app
from harness.config import Settings
from harness.core.agent import Agent
from harness.storage.sqlite import RunStore
from tests.harness_support import tool_call, wait_for_run


API_TOKEN = "approval-test-token-only"
WORKER_ID = "approval-test-worker"


class ApprovalWorker:
    def __init__(self, environment="cloud-browser", computer_targets=None, safe_bash_path=True):
        self.safe_bash_path = safe_bash_path
        self.calls = []
        self.environment = environment
        self.computer_targets = computer_targets

    async def acquire(self, worker_id, run_id, workspace_id, *, on_reserved=None):
        cloud = self.environment == "cloud-browser"
        actions = ["observe", "screenshot", "scroll", "click", "double_click", "right_click",
                   "move", "drag", "type", "key", "press"]
        if cloud:
            actions += ["navigate", "download", "upload"]
        connection = SimpleNamespace(
            worker_id=WORKER_ID,
            capabilities={"environment": self.environment, "desktop": not cloud, "bash": True,
                          "computer": True, "download": cloud, "upload": cloud, "actions": actions},
        )
        if self.safe_bash_path:
            connection.capabilities["safe_bash_path"] = True
        if self.computer_targets is not None:
            connection.capabilities["computer_targets"] = self.computer_targets
        if on_reserved is not None:
            on_reserved(connection)
        return connection

    resume = acquire

    async def release(self, worker_id, run_id):
        pass

    async def bash(self, worker_id, run_id, call_id, command, timeout):
        self.calls.append((call_id, command))
        return {"exit_code": 0}

    async def computer(self, worker_id, run_id, call_id, action):
        self.calls.append((call_id, action["action"]))
        return {}


class ApprovalModel:
    def __init__(self):
        self.results = []

    async def complete(self, messages, tools):
        self.results = [json.loads(message["content"]) for message in messages if message["role"] == "tool"]
        if self.results:
            return {"role": "assistant", "content": "Approval decisions recorded."}
        reply = tool_call("denied", "bash", {"command": "printf denied"})
        reply["tool_calls"].extend(tool_call("allowed", "bash", {"command": "printf allowed"})["tool_calls"])
        return reply


class MixedModel:
    def __init__(self, steps):
        self.steps = steps

    async def complete(self, messages, tools):
        if any(message["role"] == "tool" for message in messages):
            return {"role": "assistant", "content": "Done."}
        calls = []
        for index, (tool, args) in enumerate(self.steps):
            if tool == "computer_use":
                fields = ("url", "direction", "amount") if args["action"] in {"navigate", "scroll"} else (
                    "x", "y", "end_x", "end_y",
                )
                command = ["harness", "computer", args["action"].replace("_", "-"),
                           *(str(args[field]) for field in fields if field in args)]
                for option in ("selector", "path", "target", "x", "y"):
                    if option in args and option not in fields:
                        command.extend([f"--{option}", str(args[option])])
                tool, args = "bash", {"command": shlex.join(command)}
            calls.append(tool_call(str(index), tool, args)["tool_calls"][0])
        return {"role": "assistant", "content": None, "tool_calls": calls}


class BashApprovalTest(unittest.IsolatedAsyncioTestCase):
    async def test_bash_executes_only_the_specifically_approved_call(self):
        worker = ApprovalWorker()
        model = ApprovalModel()
        with tempfile.TemporaryDirectory() as temporary_directory:
            app = create_app(Settings(data_dir=Path(temporary_directory), api_token=API_TOKEN),
                             llm=model, worker_manager=worker)
            async with app.router.lifespan_context(app), httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver",
                headers={"Authorization": f"Bearer {API_TOKEN}"},
            ) as client:
                started = (await client.post("/v1/runs", json={"task": "Check tool approvals"})).raise_for_status().json()
                run_id = started["id"]
                approval_url = f"/v1/runs/{run_id}/approve"
                waiting = await wait_for_run(client, run_id, "awaiting_approval")
                self.assertEqual(waiting["status"], "awaiting_approval", waiting)
                denied = waiting["approval"]["call_id"]
                self.assertEqual(worker.calls, [])

                mismatched = await client.post(approval_url, json={"call_id": "different-call", "approved": True})
                self.assertEqual(mismatched.status_code, 409)
                (await client.post(approval_url, json={"call_id": denied, "approved": False})).raise_for_status()

                second = await wait_for_run(client, run_id, "awaiting_approval")
                self.assertEqual(second["status"], "awaiting_approval", second)
                allowed = second["approval"]["call_id"]
                self.assertNotEqual(denied, allowed)
                self.assertEqual(worker.calls, [])
                (await client.post(approval_url, json={"call_id": allowed, "approved": True})).raise_for_status()

                completed = await wait_for_run(client, run_id, "completed", "failed")
                self.assertEqual(completed["status"], "completed", completed)
                self.assertEqual(worker.calls, [(allowed, "printf allowed")])
                self.assertIn("declined", model.results[0]["error"])
                self.assertEqual(model.results[1]["exit_code"], 0)

    async def check_approvals(self, steps, pauses, *, mode="auto", environment="cloud-browser",
                              explicit_worker=False, trust_bash=False, automatic=(),
                              computer_targets=None, routed_targets=None, safe_bash_path=True):
        worker = ApprovalWorker(environment, computer_targets, safe_bash_path)
        expected_calls = [args.get("action") or args["command"] for _, args in steps]
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(data_dir=Path(directory), api_token=API_TOKEN, trust_cloud_bash=trust_bash)
            app = create_app(settings, llm=MixedModel(steps), worker_manager=worker)
            async with app.router.lifespan_context(app), httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver",
                headers={"Authorization": f"Bearer {API_TOKEN}"},
            ) as client:
                request = {"task": "Check tool approvals"}
                if mode is not None:
                    request["approval_mode"] = mode
                if explicit_worker:
                    request["worker_id"] = WORKER_ID
                run = (await client.post("/v1/runs", json=request)).raise_for_status().json()
                run_id = run["id"]
                self.assertEqual(run["approval_mode"], mode or "legacy")
                requested = []
                for index in pauses:
                    run = await wait_for_run(client, run_id, "awaiting_approval")
                    self.assertEqual(run["status"], "awaiting_approval", run)
                    approval = run["approval"]
                    tool, args = steps[index]
                    self.assertEqual(approval["tool"], tool)
                    if tool == "computer_use":
                        self.assertEqual(approval["entrypoint"], "bash")
                        self.assertTrue(approval["command"].startswith("harness computer "))
                    self.assertEqual({key: approval["arguments"][key] for key in args}, args)
                    if routed_targets is not None:
                        self.assertEqual(approval["arguments"].get("target"), routed_targets[index])
                    self.assertEqual([action for _, action in worker.calls], expected_calls[:index])
                    if mode == "auto":
                        self.assertIsInstance(approval["reason"], str)
                        self.assertTrue(approval["reason"].strip())
                    requested.append(approval)
                    decision = {"call_id": approval["call_id"], "approved": True}
                    (await client.post(f"/v1/runs/{run_id}/approve", json=decision)).raise_for_status()

                run = await wait_for_run(client, run_id, "completed", "awaiting_approval")
                self.assertEqual(run["status"], "completed", run)
                self.assertEqual([action for _, action in worker.calls], expected_calls)
                self.assertEqual(RunStore(Path(directory)).get(run_id)["approval_mode"], mode or "legacy")

                events = (await client.get(f"/v1/runs/{run_id}/events")).raise_for_status().json()
                self.assertEqual([event["data"] for event in events if event["type"] == "approval_requested"], requested)
                if routed_targets is not None:
                    self.assertEqual([event["data"]["arguments"].get("target") for event in events
                                      if event["type"] == "tool_call"], routed_targets)
                auto_calls = {event["data"]["call_id"]: event["data"]["auto_approval_reason"] for event in events
                              if event["type"] == "tool_call" and "auto_approval_reason" in event["data"]}
                self.assertEqual(list(auto_calls), [worker.calls[index][0] for index in automatic])
                for reason in auto_calls.values():
                    self.assertIsInstance(reason, str)
                    self.assertTrue(reason.strip())

    async def test_auto_checks_each_cloud_tool_call(self):
        await self.check_approvals([
            ("computer_use", {"action": "observe"}),
            ("computer_use", {"action": "scroll", "direction": "down", "amount": 420}),
            ("bash", {"command": "printf done"}),
            ("bash", {"command": "printf done > report.txt"}),
            ("computer_use", {"action": "navigate", "url": "https://example.test"}),
            ("computer_use", {"action": "click", "x": 8, "y": 7}),
            ("computer_use", {"action": "upload", "selector": "input[type=file]", "path": "report.txt"}),
            ("computer_use", {"action": "screenshot"}),
        ], pauses=(3, 4, 5, 6), automatic=(0, 1, 2, 7))

    async def test_auto_desktop_observation_also_works_on_default_worker(self):
        steps = [
            ("computer_use", {"action": "screenshot"}),
            ("computer_use", {"action": "observe"}),
            ("computer_use", {"action": "scroll", "direction": "down", "amount": 2}),
            ("computer_use", {"action": "click", "x": 8, "y": 7}),
            ("bash", {"command": "pwd"}),
            ("bash", {"command": "ls -la ~/Documents/ ~/Desktop/ ~/Pictures/ 2>/dev/null"}),
            ("bash", {"command": "find ~/Downloads -delete"}),
        ]
        for explicit_worker in (False, True):
            with self.subTest(explicit_worker=explicit_worker):
                await self.check_approvals(steps, (2, 3, 6), environment="macos-desktop",
                                           explicit_worker=explicit_worker, automatic=(0, 1, 4, 5), trust_bash=True)

    async def test_old_worker_does_not_auto_approve_external_view_commands(self):
        await self.check_approvals([
            ("bash", {"command": "pwd"}),
            ("bash", {"command": "ls -la ~/Documents/"}),
            ("bash", {"command": "cat README.md"}),
        ], pauses=(1, 2), environment="macos-desktop", safe_bash_path=False, automatic=(0,))

    async def test_desktop_worker_targets_the_current_desktop(self):
        await self.check_approvals([
            ("computer_use", {"action": "observe"}),
            ("computer_use", {"action": "click", "x": 8, "y": 7}),
        ], pauses=(1,), automatic=(0,), environment="macos-desktop",
            computer_targets={"desktop": ["click", "observe"]}, routed_targets=["desktop", "desktop"])

    def test_desktop_worker_does_not_accept_browser_actions(self):
        capabilities = {"environment": "macos-desktop", "computer_targets": {"desktop": ["click"]}}
        with self.assertRaisesRegex(ValueError, "does not support computer target: browser"):
            Agent._route_computer({"action": "click", "target": "browser", "x": 8, "y": 7}, capabilities)

    async def test_directional_scroll_only_auto_approves_short_browser_scrolls_without_coordinates(self):
        await self.check_approvals([
            ("computer_use", {"action": "scroll", "direction": "up", "amount": 420}),
            ("computer_use", {"action": "scroll", "direction": "right", "amount": 4_001}),
            ("computer_use", {"action": "scroll", "direction": "down", "amount": 20, "x": 8, "y": 7}),
            ("computer_use", {"action": "drag", "x": 8, "y": 7, "end_x": 16, "end_y": 14}),
        ], pauses=(1, 2, 3), automatic=(0,))

    def test_tool_arguments_validate_gesture_fields_and_keep_the_reviewed_timeout(self):
        agent = Agent(Settings(), None, None, None, None)
        bash = {"name": "bash", "arguments": '{"command": "pwd"}'}
        self.assertEqual(agent._arguments(bash), {"command": "pwd", "timeout": 120})
        old_request = {"arguments": {"command": "pwd", "timeout": 30}}
        self.assertEqual(agent._arguments(bash, old_request), old_request["arguments"])
        capped_agent = Agent(Settings(max_tool_seconds=60), None, None, None, None)
        self.assertEqual(capped_agent._arguments(bash)["timeout"], 60)

        invalid_actions = [
            {"action": "drag", "x": 8, "y": 7, "end_x": 16},
            {"action": "key"},
            {"action": "scroll", "direction": "up"},
            {"action": "scroll", "direction": "down", "amount": 10, "dy": 10},
        ]
        for args in invalid_actions:
            with self.subTest(args=args), self.assertRaises(ValueError):
                agent._arguments({"name": "computer_use", "arguments": json.dumps(args)})

    async def test_manual_allow_and_cloud_bash_trust(self):
        steps = [("bash", {"command": "printf done > report.txt"})]
        for mode, trust_bash, pauses, automatic in (
            ("manual", True, (0,), ()),
            ("allow", False, (), ()),
            ("auto", True, (), (0,)),
            (None, True, (), ()),
        ):
            with self.subTest(mode=mode):
                await self.check_approvals(steps, pauses, mode=mode, trust_bash=trust_bash, automatic=automatic)

    async def test_trusted_cloud_bash_does_not_authorize_cli_mouse_input(self):
        await self.check_approvals([
            ("bash", {"command": "printf done > report.txt"}),
            ("computer_use", {"action": "click", "x": 8, "y": 7}),
        ], pauses=(1,), automatic=(0,), trust_bash=True)
