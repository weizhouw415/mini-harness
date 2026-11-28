import asyncio
import tempfile
import unittest
from pathlib import Path

import httpx

from harness.api.server import create_app
from harness.config import Settings
from tests.harness_support import wait_for_run
from tests.test_approvals import API_TOKEN, ApprovalWorker, MixedModel


class BlockingWorker(ApprovalWorker):
    def __init__(self):
        super().__init__()
        self.started = asyncio.Event()
        self.finish = asyncio.Event()

    async def bash(self, worker_id, run_id, call_id, command, timeout):
        result = await super().bash(worker_id, run_id, call_id, command, timeout)
        if len(self.calls) == 1:
            self.started.set()
            await self.finish.wait()
        return result


class LiveApprovalTest(unittest.IsolatedAsyncioTestCase):
    async def start(self, worker, mode, *commands):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        settings = Settings(data_dir=Path(directory), api_token=API_TOKEN)
        app = create_app(settings, llm=MixedModel([("bash", {"command": command}) for command in commands]),
                         worker_manager=worker)
        await self.enterAsyncContext(app.router.lifespan_context(app))
        client = await self.enterAsyncContext(httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver",
            headers={"Authorization": f"Bearer {API_TOKEN}"},
        ))
        response = await client.post("/v1/runs", json={"task": "Check live approval", "approval_mode": mode})
        return client, response.raise_for_status().json()["id"]

    async def change_mode(self, client, run_id, mode):
        return await client.patch(f"/v1/runs/{run_id}/approval-mode", json={"approval_mode": mode})

    async def events(self, client, run_id, kind):
        response = await client.get(f"/v1/runs/{run_id}/events")
        return [event["data"] for event in response.raise_for_status().json() if event["type"] == kind]

    async def test_awaiting_call_continues_when_mode_changes_to_allow(self):
        worker = ApprovalWorker()
        command = "printf content > note.txt"
        client, run_id = await self.start(worker, "manual", command)
        waiting = await wait_for_run(client, run_id, "awaiting_approval")
        self.assertEqual(waiting["status"], "awaiting_approval", waiting)
        call_id = waiting["approval"]["call_id"]
        self.assertEqual(worker.calls, [])

        self.assertEqual((await self.change_mode(client, run_id, "unrecognized")).status_code, 422)
        updated = (await self.change_mode(client, run_id, "allow")).raise_for_status().json()
        self.assertEqual(updated["approval_mode"], "allow")
        completed = await wait_for_run(client, run_id, "completed", "awaiting_approval")
        self.assertEqual(completed["status"], "completed", completed)
        self.assertEqual(completed["worker_id"], waiting["worker_id"])
        self.assertEqual(worker.calls, [(call_id, command)])
        self.assertEqual(await self.events(client, run_id, "approval_mode_changed"), [
            {"previous_mode": "manual", "approval_mode": "allow"},
        ])
        self.assertEqual((await self.change_mode(client, run_id, "manual")).status_code, 409)

    async def test_stricter_mode_applies_to_the_next_call_in_the_same_batch(self):
        worker = BlockingWorker()
        client, run_id = await self.start(worker, "allow", "printf first", "printf second")
        await asyncio.wait_for(worker.started.wait(), timeout=5)
        first_call, = worker.calls
        try:
            updated = (await self.change_mode(client, run_id, "manual")).raise_for_status().json()
            self.assertEqual(updated["status"], "running")
            self.assertEqual(updated["approval_mode"], "manual")
            self.assertEqual(worker.calls, [first_call])
        finally:
            worker.finish.set()

        waiting = await wait_for_run(client, run_id, "awaiting_approval")
        self.assertEqual(waiting["status"], "awaiting_approval", waiting)
        self.assertEqual(waiting["approval"]["arguments"]["command"], "printf second")
        self.assertEqual(worker.calls, [first_call])
        response = await client.post(f"/v1/runs/{run_id}/approve", json={
            "call_id": waiting["approval"]["call_id"], "approved": False,
        })
        response.raise_for_status()
        completed = await wait_for_run(client, run_id, "completed")
        self.assertEqual(completed["status"], "completed", completed)
        self.assertEqual(worker.calls, [first_call])

    async def test_automatic_mode_keeps_an_unsafe_call_pending(self):
        worker = ApprovalWorker()
        command = "rm -rf -- project-directory"
        client, run_id = await self.start(worker, "manual", command)
        waiting = await wait_for_run(client, run_id, "awaiting_approval")
        self.assertEqual(waiting["status"], "awaiting_approval", waiting)
        original = waiting["approval"]

        (await self.change_mode(client, run_id, "auto")).raise_for_status()
        updated = await wait_for_run(client, run_id, "awaiting_approval")
        self.assertEqual(updated["status"], "awaiting_approval", updated)
        self.assertEqual(updated["approval_mode"], "auto")
        self.assertEqual(updated["approval"]["call_id"], original["call_id"])
        self.assertEqual(updated["approval"]["arguments"], original["arguments"])
        self.assertTrue(updated["approval"]["reason"])
        self.assertEqual(worker.calls, [])

        unchanged = (await self.change_mode(client, run_id, "auto")).raise_for_status().json()
        self.assertEqual(unchanged["approval"], updated["approval"])
        self.assertEqual(len(await self.events(client, run_id, "approval_mode_changed")), 1)
