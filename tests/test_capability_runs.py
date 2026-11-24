import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import httpx

from harness.api.server import create_app
from harness.config import Settings
from harness.execution.router import WorkerDisconnected
from tests.harness_support import tool_call, wait_for_run


LOCAL = {"Sec-Fetch-Site": "same-origin", "X-Harness-UI": "1"}
OFF = {"view": False, "register": False, "computer_use": False}
ON = {name: True for name in OFF}


class RecordingWorker:
    def __init__(self, *, toggles=True, disconnect=False):
        self.toggles, self.disconnect = toggles, disconnect
        self.calls = []

    async def acquire(self, worker_id, run_id, workspace_id, *, on_reserved=None):
        capabilities = {"environment": "cloud-browser", "bash": True, "computer": True,
                        "computer_cli": True, "actions": ["click"], "artifact_workspace": True}
        if self.toggles:
            capabilities["cli_capability_toggles"] = True
        worker = SimpleNamespace(worker_id="worker", capabilities=capabilities)
        if on_reserved:
            on_reserved(worker)
        return worker

    async def resume(self, worker_id, run_id, workspace_id):
        return await self.acquire(worker_id, run_id, workspace_id)

    async def release(self, worker_id, run_id):
        pass

    async def bash(self, worker_id, run_id, call_id, command, timeout, *, cli_capabilities=None):
        self.calls.append(cli_capabilities)
        if self.disconnect:
            self.disconnect = False
            raise WorkerDisconnected(worker_id)
        return {"exit_code": 0, "stdout": "done", "stderr": ""}


class RecordingModel:
    def __init__(self):
        self.requests = []

    async def complete(self, messages, tools):
        self.requests.append((messages[0]["content"], tools))
        if any(message["role"] == "tool" for message in messages):
            return {"content": "done"}
        return tool_call("shell", "bash", {"command": "printf done"})


class CapabilityRunTest(unittest.IsolatedAsyncioTestCase):
    async def test_snapshot_survives_pause_and_changes_apply_to_next_run(self):
        with tempfile.TemporaryDirectory() as directory:
            worker, model = RecordingWorker(disconnect=True), RecordingModel()
            app = create_app(Settings(data_dir=Path(directory), api_token="token"),
                             worker_manager=worker, llm=model, local_ui=True)
            async with app.router.lifespan_context(app), httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1",
                headers={**LOCAL, "Authorization": "Bearer token"},
            ) as client:
                (await client.put("/ui/capabilities", json=OFF)).raise_for_status()
                first = (await client.post("/v1/runs", json={"task": "First", "approval_mode": "allow"})).json()
                paused = await wait_for_run(client, first["id"], "paused")
                self.assertEqual(paused["status"], "paused", paused)
                self.assertEqual(worker.calls, [OFF])
                (await client.put("/ui/capabilities", json=ON)).raise_for_status()
                (await client.post(f"/v1/runs/{first['id']}/resume")).raise_for_status()
                completed = await wait_for_run(client, first["id"], "completed")
                self.assertEqual(completed["status"], "completed", completed)
                self.assertEqual(len(model.requests), 2)
                for system, tools in model.requests:
                    combined = system + json.dumps(tools)
                    for hidden in ("harness view", "harness register", "harness computer", "computer_use"):
                        self.assertNotIn(hidden, combined)
                    self.assertEqual([tool["function"]["name"] for tool in tools], ["bash"])
                events = app.state.store.events(first["id"], include_deltas=False)
                self.assertEqual([event["data"] for event in events if event["type"] == "capability_snapshot"], [OFF])

                second = (await client.post("/v1/runs", json={"task": "Second", "approval_mode": "allow"})).json()
                completed = await wait_for_run(client, second["id"], "completed")
                self.assertEqual(completed["status"], "completed", completed)
                self.assertIn("harness computer", model.requests[-1][0])
                self.assertIsNone(worker.calls[-1])  # Default-enabled requests remain compatible with older workers.

    async def test_legacy_worker_fails_closed_when_any_capability_is_disabled(self):
        with tempfile.TemporaryDirectory() as directory:
            worker, model = RecordingWorker(toggles=False), RecordingModel()
            app = create_app(Settings(data_dir=Path(directory), api_token="token"),
                             worker_manager=worker, llm=model, local_ui=True)
            async with app.router.lifespan_context(app), httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1",
                headers={**LOCAL, "Authorization": "Bearer token"},
            ) as client:
                (await client.put("/ui/capabilities", json={**ON, "view": False})).raise_for_status()
                run = (await client.post("/v1/runs", json={"task": "Legacy"})).json()
                failed = await wait_for_run(client, run["id"], "failed")
                self.assertEqual(failed["status"], "failed", failed)
                self.assertIn("Update the Worker", failed["error"])
                self.assertEqual(worker.calls, [])
                self.assertEqual(model.requests, [])
