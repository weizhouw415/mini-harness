import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import httpx

from harness.api.server import create_app
from harness.config import Settings
from harness.storage.sqlite import RunStore
from tests.harness_support import tool_call, wait_for_run


API_TOKEN = "approval-test-token-only"
WORKER_ID = "approval-test-worker"


class ApprovalWorker:
    def __init__(self, environment="cloud-browser"):
        self.calls = []
        self.environment = environment

    async def acquire(self, worker_id, run_id, workspace_id, *, on_reserved=None):
        cloud = self.environment == "cloud-browser"
        actions = ["observe", "screenshot", "scroll", "click", "type", "press"]
        if cloud:
            actions += ["navigate", "download", "upload"]
        connection = SimpleNamespace(
            worker_id=WORKER_ID,
            capabilities={"environment": self.environment, "desktop": not cloud, "bash": True,
                          "computer": True, "download": cloud, "upload": cloud, "actions": actions},
        )
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
        calls = [tool_call(str(index), tool, args)["tool_calls"][0] for index, (tool, args) in enumerate(self.steps)]
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
                              explicit_worker=False, trust_bash=False, automatic=()):
        worker = ApprovalWorker(environment)
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
                    self.assertEqual({key: approval["arguments"][key] for key in args}, args)
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
                auto_calls = {event["data"]["call_id"]: event["data"]["auto_approval_reason"] for event in events
                              if event["type"] == "tool_call" and "auto_approval_reason" in event["data"]}
                self.assertEqual(list(auto_calls), [worker.calls[index][0] for index in automatic])
                for reason in auto_calls.values():
                    self.assertIsInstance(reason, str)
                    self.assertTrue(reason.strip())

    async def test_auto_checks_each_cloud_tool_call(self):
        await self.check_approvals([
            ("computer_use", {"action": "observe"}),
            ("computer_use", {"action": "scroll", "dy": 420}),
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
            ("computer_use", {"action": "scroll", "dy": 2}),
            ("computer_use", {"action": "click", "x": 8, "y": 7}),
            ("bash", {"command": "pwd"}),
            ("bash", {"command": "ls"}),
        ]
        for explicit_worker in (False, True):
            with self.subTest(explicit_worker=explicit_worker):
                await self.check_approvals(steps, (2, 3, 5), environment="macos-desktop",
                                           explicit_worker=explicit_worker, automatic=(0, 1, 4), trust_bash=True)

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
