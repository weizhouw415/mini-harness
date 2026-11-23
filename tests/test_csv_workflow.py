import asyncio
import json
import os
import shutil
import socket
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import uvicorn
from fastapi import Request

from harness.api.server import create_app
from harness.config import Settings
from harness.execution.router import WorkerManager
from tests.csv_fixture import CsvFixture
from tests.harness_support import eventually, tool_call, wait_for_run
from workers.cloud.worker import WorkerBackend
from workers.common.transport import serve_worker


API_TOKEN = "csv-acceptance-token-only"
WORKER_SECRET = "csv-acceptance-worker-secret-only"
WORKER_ID = "csv-test-cloud"
PROVIDER_MODEL = "scripted-csv-test-model"
PROVIDER_TOKEN = "csv-acceptance-provider-token-only"
CSV_TASK = "Sign in to the inventory fixture, double each CSV quantity, and upload the result."
CSV_FOLLOWUP = "Repeat the CSV inventory upload from the previous run."
CSV_RESULT = "The processed inventory CSV was uploaded."
SKILL_GUIDANCE = "Use CSV parsing to double the inventory quantity column before uploading."
CSV_SKILL = f"""---
name: csv-inventory
description: Process and upload a CSV inventory export.
version: 1.0.0
environments:
  - cloud-browser
---
# CSV inventory

{SKILL_GUIDANCE}
"""

PROCESS_INVENTORY = """python3 - <<'PY'
import csv
with open('source.csv', newline='') as source, open('processed.csv', 'w', newline='') as destination:
    rows = csv.DictReader(source)
    writer = csv.DictWriter(destination, fieldnames=['item', 'quantity'])
    writer.writeheader()
    for row in rows:
        writer.writerow({'item': row['item'], 'quantity': int(row['quantity']) * 2})
PY"""


def csv_calls(fixture_url: str):
    return [
        ("computer_use", {"action": "navigate", "url": fixture_url}),
        ("computer_use", {"action": "screenshot"}),
        ("computer_use", {"action": "type", "selector": "#username", "text": "fixture-user"}),
        ("computer_use", {"action": "type", "selector": "#password", "text": "fixture-password"}),
        ("computer_use", {"action": "click", "selector": "#login"}),
        ("computer_use", {"action": "download", "selector": "#download", "path": "source.csv"}),
        ("bash", {"command": PROCESS_INVENTORY, "timeout": 10}),
        ("computer_use", {"action": "upload", "selector": "#upload", "path": "processed.csv"}),
        ("computer_use", {"action": "click", "selector": "#submit"}),
    ]


class ScriptedModel:
    def __init__(self, fixture_url: str):
        self.calls = csv_calls(fixture_url)
        self.next_call = {CSV_TASK: 0, CSV_FOLLOWUP: 0}
        self.saw_image = False
        self.saw_previous_response = False
        self.saw_reusable_skill = False

    async def complete(self, messages, tools):
        if not tools:
            if "double each CSV quantity" not in messages[-1]["content"]:
                raise AssertionError("Learning did not receive the completed CSV trace")
            return {"role": "assistant", "content": CSV_SKILL}

        exposed_tools = {tool["function"]["name"] for tool in tools}
        if exposed_tools != {"bash", "computer_use"}:
            raise AssertionError(f"Unexpected model tools: {exposed_tools}")

        task = next(message["content"] for message in reversed(messages)
                    if message["role"] == "user" and isinstance(message.get("content"), str))
        index = self.next_call[task]
        if index == 2:
            self.saw_image |= any(
                isinstance(message.get("content"), list)
                and any(
                    block.get("type") == "image_url"
                    and block["image_url"]["url"].startswith("data:image/png;base64,")
                    for block in message["content"]
                )
                for message in messages
            )

        if task == CSV_FOLLOWUP and index == 0:
            self.saw_previous_response = any(
                message["role"] == "assistant" and message.get("content") == CSV_RESULT for message in messages
            )
            self.saw_reusable_skill = SKILL_GUIDANCE in messages[0]["content"]
        if index == len(self.calls):
            return {"role": "assistant", "content": CSV_RESULT}

        name, arguments = self.calls[index]
        self.next_call[task] += 1
        return tool_call(f"csv-call-{index}", name, arguments)


async def evaluate_held_out_csv(skill, environment, backend, fixture, fixture_url):
    # Fixture-specific evaluator. The candidate is inspected as data, never executed.
    if environment != "cloud-browser" or SKILL_GUIDANCE not in skill["content"]:
        return {"passed": False, "details": "Required CSV processing guidance missing"}
    try:
        await backend.open_run("held-out-run", "held-out-workspace")
        for name, arguments in csv_calls(fixture_url):
            if name == "bash":
                result = await backend.bash("held-out-run", **arguments)
                if result["exit_code"] != 0:
                    return {"passed": False, "details": "CSV processing failed"}
            else:
                await backend.computer("held-out-run", arguments)
        await eventually(lambda: fixture.uploads)
        return {"passed": fixture.uploads == [fixture.expected_rows], "details": "Held-out CSV upload"}
    finally:
        await backend.close()


@asynccontextmanager
async def live_server(app, listener: socket.socket):
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", timeout_graceful_shutdown=2))
    task = asyncio.create_task(server.serve(sockets=[listener]))

    async def ready():
        if task.done():
            await task
            raise AssertionError("HTTP server exited before it started")
        return server.started

    try:
        await eventually(ready)
        yield
    finally:
        server.should_exit = True
        await task


@asynccontextmanager
async def connected_worker(backend, address, *, reconnect=False):
    worker = asyncio.create_task(
        serve_worker(
            backend,
            url=f"ws://{address}/v1/workers/ws",
            worker_id=WORKER_ID,
            secret=WORKER_SECRET,
            reconnect=reconnect,
        )
    )
    try:
        yield worker
    finally:
        if not worker.done():
            worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)


class CsvWorkflowTest(unittest.IsolatedAsyncioTestCase):
    async def test_cloud_run_download_process_upload_and_resume_conversation(self):
        try:
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        except PermissionError:
            self.skipTest("Loopback sockets are disabled in this environment")

        try:
            from playwright.async_api import async_playwright
        except ImportError:
            listener.close()
            self.skipTest("Playwright is not installed")

        with listener, tempfile.TemporaryDirectory() as temporary_directory:
            executable = os.getenv("MINI_CLOUD_BROWSER_EXECUTABLE") or shutil.which("chromium")
            if not executable:
                async with async_playwright() as playwright:
                    if not Path(playwright.chromium.executable_path).is_file():
                        self.skipTest("Chromium is unavailable; run python -m playwright install chromium")

            listener.bind(("127.0.0.1", 0))
            address = f"127.0.0.1:{listener.getsockname()[1]}"
            model = ScriptedModel(f"http://{address}/fixture/")
            fixture = CsvFixture()
            held_out = CsvFixture((("green, apples", 3), ("plums", 0)))
            data_dir = Path(temporary_directory)
            settings = Settings(
                data_dir=data_dir / "server",
                api_token=API_TOKEN,
                worker_secret=WORKER_SECRET,
                model=PROVIDER_MODEL,
                llm_base_url=f"http://{address}/provider/v1",
                llm_api_key=PROVIDER_TOKEN,
                trust_cloud_bash=True,
            )

            async def evaluator(skill, environment):
                evaluation_backend = WorkerBackend(workspace_root=data_dir / "evaluation", executable_path=executable)
                return await evaluate_held_out_csv(
                    skill, environment, evaluation_backend, held_out, f"http://{address}/held-out/"
                )

            app = create_app(settings, skill_evaluator=evaluator)
            app.mount("/fixture", fixture.app)
            app.mount("/held-out", held_out.app)

            @app.post("/provider/v1/chat/completions")
            async def provider(request: Request):
                self.assertEqual(request.headers.get("Authorization"), f"Bearer {PROVIDER_TOKEN}")
                payload = await request.json()
                self.assertEqual(payload["model"], PROVIDER_MODEL)
                message = await model.complete(payload["messages"], payload.get("tools", []))
                return {"choices": [{"message": message}], "usage": {"total_tokens": 1}}

            backend = WorkerBackend(workspace_root=data_dir / "worker", executable_path=executable)

            async with live_server(app, listener), httpx.AsyncClient(
                base_url=f"http://{address}", headers={"Authorization": f"Bearer {API_TOKEN}"}
            ) as client, connected_worker(backend, address) as worker:
                await eventually(lambda: self.worker_is_ready(client, worker))
                first_run = await self.start_and_wait(client, CSV_TASK)
                await eventually(lambda: fixture.uploads)
                self.assertEqual(fixture.login_count, 1)
                self.assertEqual(fixture.uploads, [fixture.expected_rows])
                self.assertTrue(model.saw_image, "The model did not receive the screenshot as an image")

                events = (await client.get(f"/v1/runs/{first_run['id']}/events")).raise_for_status().json()
                event_types = [event["type"] for event in events]
                self.assertEqual(event_types.count("tool_call"), len(model.calls))
                self.assertEqual(event_types.count("tool_result"), len(model.calls))
                self.assertEqual(first_run["worker_id"], WORKER_ID)

                screenshot = next(
                    event["data"]["result"]["artifact"] for event in events
                    if event["type"] == "tool_result" and "artifact" in event["data"]["result"]
                )
                artifact = (await client.get(f"/v1/runs/{first_run['id']}/artifacts/{screenshot}")).raise_for_status()
                self.assertTrue(artifact.content.startswith(b"\x89PNG\r\n\x1a\n"))

                candidate = (await client.post(f"/v1/runs/{first_run['id']}/learn")).raise_for_status().json()
                self.assertEqual(candidate["state"], "candidate")
                skill_url = f"/v1/skills/{candidate['name']}/{candidate['version']}"
                evaluation = (await client.post(
                    f"{skill_url}/evaluate", json={"environment": "cloud-browser"}
                )).raise_for_status().json()
                self.assertTrue(evaluation["evaluations"]["cloud-browser"]["passed"], evaluation)
                self.assertEqual(held_out.uploads, [held_out.expected_rows])
                published = (await client.post(f"{skill_url}/publish", json={"approved": True})).raise_for_status().json()
                self.assertEqual(published["state"], "active")

                await eventually(lambda: self.worker_is_ready(client, worker))
                followup = await self.start_and_wait(client, CSV_FOLLOWUP, first_run["session_id"])
                self.assertNotEqual(followup["id"], first_run["id"])
                self.assertEqual(followup["session_id"], first_run["session_id"])
                self.assertTrue(model.saw_previous_response)
                self.assertTrue(model.saw_reusable_skill)
                await eventually(lambda: len(fixture.uploads) == 2)
                self.assertEqual(fixture.login_count, 2)

    async def worker_is_ready(self, client, worker):
        if worker.done():
            await worker
            self.fail("Worker exited before registering")
        workers = (await client.get("/v1/workers")).raise_for_status().json()
        return any(row["worker_id"] == WORKER_ID and row["connected"] and not row["run_id"] for row in workers)

    async def start_and_wait(self, client, task, session_id=None):
        request = {"task": task, "worker_id": WORKER_ID}
        if session_id is not None:
            request["session_id"] = session_id
        response = await client.post("/v1/runs", json=request)
        self.assertEqual(response.status_code, 202, response.text)
        run_id = response.json()["id"]

        result = await wait_for_run(client, run_id, "completed", "failed", "paused", "cancelled")
        self.assertEqual(result["status"], "completed", result)
        return result


class TrackingWorkerManager(WorkerManager):
    def __init__(self):
        super().__init__(WORKER_SECRET)
        self.socket = None

    async def accept(self, websocket):
        self.socket = websocket
        await super().accept(websocket)


class InterruptedBackend:
    def __init__(self):
        self.clicked = asyncio.Event()
        self.unblock_click = asyncio.Event()
        self.opens = []
        self.actions = []

    def capabilities(self):
        return {"environment": "cloud-browser", "computer": True, "bash": False}

    async def open_run(self, run_id, workspace_id):
        self.opens.append((run_id, workspace_id))

    async def close_run(self, run_id):
        pass

    async def computer(self, run_id, action):
        self.actions.append(action["action"])
        if action["action"] == "click":
            self.clicked.set()
            await self.unblock_click.wait()
        return {"ok": True}


class RecoveryModel:
    def __init__(self):
        self.saw_unknown_outcome = False

    async def complete(self, messages, tools):
        observations = [json.loads(message["content"]) for message in messages if message["role"] == "tool"]
        if not observations:
            return tool_call("first", "computer_use", {"action": "click", "x": 0, "y": 0})
        if len(observations) == 1:
            self.saw_unknown_outcome = observations[0].get("outcome_unknown") is True
            return tool_call("inspect", "computer_use", {"action": "observe"})
        return {"role": "assistant", "content": "Inspected worker state after the interruption."}


class WorkerRecoveryTest(unittest.IsolatedAsyncioTestCase):
    async def test_resumes_same_worker_without_replaying_ambiguous_call(self):
        try:
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        except PermissionError:
            self.skipTest("Loopback sockets are disabled in this environment")

        with listener, tempfile.TemporaryDirectory() as temporary_directory:
            listener.bind(("127.0.0.1", 0))
            address = f"127.0.0.1:{listener.getsockname()[1]}"
            manager = TrackingWorkerManager()
            model = RecoveryModel()
            settings = Settings(data_dir=Path(temporary_directory), api_token=API_TOKEN, worker_secret=WORKER_SECRET)
            app = create_app(settings, llm=model, worker_manager=manager)
            backend = InterruptedBackend()

            async with live_server(app, listener), httpx.AsyncClient(
                base_url=f"http://{address}", headers={"Authorization": f"Bearer {API_TOKEN}"}
            ) as client, connected_worker(backend, address, reconnect=True):
                await eventually(lambda: any(row["connected"] for row in manager.list_workers()))
                started = await client.post("/v1/runs", json={"task": "Click and inspect the fixture"})
                self.assertEqual(started.status_code, 202, started.text)
                original = started.json()
                run_url = f"/v1/runs/{original['id']}"
                await asyncio.wait_for(backend.clicked.wait(), timeout=10)

                await manager.socket.close(code=1012)
                paused = await wait_for_run(client, original["id"], "paused", "failed")
                self.assertEqual(paused["status"], "paused", paused)
                self.assertEqual(paused["worker_id"], WORKER_ID)
                self.assertEqual(backend.actions, ["click"])
                backend.unblock_click.set()

                await eventually(lambda: any(row["connected"] for row in manager.list_workers()))
                resumed = await client.post(f"{run_url}/resume")
                self.assertEqual(resumed.status_code, 202, resumed.text)
                completed = await wait_for_run(client, original["id"], "completed", "failed", "paused")
                self.assertEqual(completed["status"], "completed", completed)
                self.assertEqual(completed["worker_id"], WORKER_ID)
                self.assertEqual(backend.opens, [(original["id"], original["workspace_id"])])
                self.assertEqual(backend.actions, ["click", "observe"])
                self.assertTrue(model.saw_unknown_outcome)

