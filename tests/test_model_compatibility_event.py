import tempfile
import unittest
from pathlib import Path

import httpx

from harness.api.server import create_app
from harness.config import Settings
from tests.harness_support import tool_call, wait_for_run
from tests.test_capability_runs import RecordingWorker


class CompatibilityModel:
    def __init__(self):
        self.messages = []

    async def complete(self, messages, tools):
        self.messages.append(messages)
        reply = ({"content": "done"} if any(item["role"] == "tool" for item in messages)
                 else tool_call("shell", "bash", {"command": "printf done"}))
        reply["_compatibility"] = {"reasoning_effort": "none"}
        return reply


class ModelCompatibilityEventTest(unittest.IsolatedAsyncioTestCase):
    async def test_notice_is_recorded_once_and_excluded_from_model_context(self):
        with tempfile.TemporaryDirectory() as directory:
            worker, model = RecordingWorker(), CompatibilityModel()
            app = create_app(Settings(data_dir=Path(directory), api_token="token"),
                             worker_manager=worker, llm=model)
            async with app.router.lifespan_context(app), httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1",
                headers={"Authorization": "Bearer token"},
            ) as client:
                run = (await client.post("/v1/runs", json={"task": "Test", "approval_mode": "allow"})).json()
                completed = await wait_for_run(client, run["id"], "completed")
                self.assertEqual(completed["status"], "completed")
                events = (await client.get(f"/v1/runs/{run['id']}/events")).json()
                notices = [event["data"] for event in events if event["type"] == "model_compatibility"]
                self.assertEqual(notices, [{"reasoning_effort": "none"}])
                self.assertNotIn("model_compatibility", str(model.messages[-1]))
