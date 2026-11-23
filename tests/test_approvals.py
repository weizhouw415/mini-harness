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
        connection = SimpleNamespace(
            worker_id=WORKER_ID, capabilities={"environment": self.environment, "bash": True},
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

    async def test_approval_modes_control_when_tools_run(self):
        class MixedModel:
            async def complete(self, messages, tools):
                if any(message["role"] == "tool" for message in messages):
                    return {"role": "assistant", "content": "Done."}
                reply = tool_call("observe", "computer_use", {"action": "observe"})
                reply["tool_calls"].extend(tool_call(
                    "navigate", "computer_use", {"action": "navigate", "url": "https://example.test"},
                )["tool_calls"])
                reply["tool_calls"].extend(tool_call("bash", "bash", {"command": "printf done"})["tool_calls"])
                return reply

        for mode, environment, trust_bash, expected_pauses in (
            ("manual", "cloud-browser", True, ("observe", "navigate", "printf done")),
            ("auto", "cloud-browser", False, ("navigate", "printf done")),
            ("auto", "cloud-browser", True, ("navigate",)),
            ("auto", "macos-desktop", True, ("observe", "navigate", "printf done")),
            ("allow", "cloud-browser", False, ()),
        ):
            with self.subTest(mode=mode, environment=environment, trust_bash=trust_bash):
                worker = ApprovalWorker(environment)
                with tempfile.TemporaryDirectory() as directory:
                    settings = Settings(data_dir=Path(directory), api_token=API_TOKEN, trust_cloud_bash=trust_bash)
                    app = create_app(settings, llm=MixedModel(), worker_manager=worker)
                    async with app.router.lifespan_context(app), httpx.AsyncClient(
                        transport=httpx.ASGITransport(app=app), base_url="http://testserver",
                        headers={"Authorization": f"Bearer {API_TOKEN}"},
                    ) as client:
                        response = await client.post("/v1/runs", json={"task": "Check modes", "approval_mode": mode})
                        run = response.raise_for_status().json()
                        self.assertEqual(run["approval_mode"], mode)
                        for paused_action in expected_pauses:
                            run = await wait_for_run(client, run["id"], "awaiting_approval")
                            self.assertEqual(run["status"], "awaiting_approval", run)
                            approval = run["approval"]
                            args = approval["arguments"]
                            self.assertEqual(args.get("action") or args.get("command"), paused_action)
                            self.assertNotIn(paused_action, [action for _, action in worker.calls])
                            approval_url = f"/v1/runs/{run['id']}/approve"
                            decision = {"call_id": approval["call_id"], "approved": True}
                            (await client.post(approval_url, json=decision)).raise_for_status()
                        run = await wait_for_run(client, run["id"], "completed", "awaiting_approval")
                        self.assertEqual(run["status"], "completed", run)
                        self.assertEqual([action for _, action in worker.calls], ["observe", "navigate", "printf done"])
                        self.assertEqual(RunStore(Path(directory)).get(run["id"])["approval_mode"], mode)
