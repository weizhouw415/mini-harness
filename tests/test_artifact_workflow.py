import asyncio
import base64
import io
import json
import shlex
import socket
import tempfile
import unittest
from pathlib import Path
from urllib.parse import quote

import httpx
from fastapi import Request
from PIL import Image

from harness.api.server import create_app
from harness.config import Settings
from tests.harness_support import eventually, tool_call, wait_for_run
from tests.test_csv_workflow import (
    API_TOKEN,
    WORKER_ID,
    WORKER_SECRET,
    connected_worker,
    live_server,
    streamed_completion,
)
from workers.cloud.worker import WorkerBackend


def image_bytes(size=(15, 10)):
    data = io.BytesIO()
    with Image.new("RGB", size, "blue") as image:
        image.save(data, "PNG")
    return data.getvalue()


def visual_dimensions(message):
    return [Image.open(io.BytesIO(base64.b64decode(block["image_url"]["url"].split(",", 1)[1]))).size
            for block in message["content"] if block["type"] == "image_url"]


class ArtifactWorkflowTest(unittest.IsolatedAsyncioTestCase):
    async def test_upload_errors_are_bounded_and_authenticated(self):
        with tempfile.TemporaryDirectory() as directory:
            app = create_app(Settings(data_dir=Path(directory), api_token=API_TOKEN, worker_secret=WORKER_SECRET))
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://harness") as client:
                self.assertEqual((await client.post("/v1/uploads")).status_code, 401)
                headers = {"Authorization": f"Bearer {API_TOKEN}", "Content-Type": "application/octet-stream",
                           "X-File-Name": quote("../example.txt", safe="")}
                invalid = await client.post("/v1/uploads", headers=headers, content=b"hello")
                self.assertEqual(invalid.status_code, 422)
                headers["X-File-Name"] = "example.txt"
                too_large = await client.post("/v1/uploads", headers=headers, content=b"x" * (20 * 1024 * 1024 + 1))
                self.assertEqual(too_large.status_code, 413)
                unknown = await client.post("/v1/runs", headers=headers | {"Content-Type": "application/json"},
                                            json={"upload_ids": ["a" * 32]})
                self.assertEqual(unknown.status_code, 404)
                self.assertEqual(app.state.store.recent(), [])

    async def test_model_sees_uploaded_and_generated_images_and_user_can_download(self):
        with socket.socket() as listener, tempfile.TemporaryDirectory() as directory:
            try:
                listener.bind(("127.0.0.1", 0))
            except PermissionError:
                self.skipTest("Loopback listening is unavailable")
            root = Path(directory)
            address = f"127.0.0.1:{listener.getsockname()[1]}"
            settings = Settings(data_dir=root / "data", api_token=API_TOKEN, worker_secret=WORKER_SECRET,
                                model="vision-fixture", llm_base_url=f"http://{address}/provider")
            app = create_app(settings)
            provider_calls = []
            reasoning = "核对附件和生成的局部预览。"
            continue_after_reasoning = asyncio.Event()

            @app.post("/provider/chat/completions")
            async def scripted_provider(request: Request):
                body = await request.json()
                self.assertIs(body["stream"], True)
                self.assertEqual({tool["function"]["name"] for tool in body["tools"]}, {"bash"})
                messages = body["messages"]
                self.assertEqual(visual_dimensions(messages[1]), [(15, 10)])
                if not provider_calls:
                    metadata = next(block["text"].removeprefix("Uploaded file: ") for block in messages[1]["content"]
                                    if block.get("text", "").startswith("Uploaded file: "))
                    source = shlex.quote(json.loads(metadata)["path"])
                    message = tool_call("view", "bash", {"command": f"cp {source} chart.png && "
                                         "harness view chart.png --crop 2,1,5,4"})
                    registration = tool_call("register", "bash", {"command": "harness register chart.png"})
                    message["tool_calls"].extend(registration["tool_calls"])
                    message["reasoning_content"] = reasoning
                else:
                    self.assertEqual([item["role"] for item in messages[-3:]], ["tool", "tool", "user"])
                    self.assertEqual(visual_dimensions(messages[-1]), [(5, 4)])
                    self.assertIn("Harness observation", messages[-1]["content"][0]["text"])
                    message = {"role": "assistant", "content": "Visual verification finished."}
                provider_calls.append(message)
                return streamed_completion(
                    message, usage={"total_tokens": 3},
                    continue_after_reasoning=continue_after_reasoning if len(provider_calls) == 1 else None,
                )

            original = image_bytes()
            headers = {"Authorization": f"Bearer {API_TOKEN}"}
            backend = WorkerBackend(workspace_root=root / "worker")
            async with live_server(app, listener), connected_worker(backend, address):
                async with httpx.AsyncClient(base_url=f"http://{address}", headers=headers) as client:
                    await eventually(lambda: app.state.workers.list_workers())
                    attachment = (await client.post("/v1/uploads", content=original, headers={
                        "Content-Type": "application/octet-stream", "X-File-Name": quote("参照图.png"),
                    })).raise_for_status().json()
                    run = (await client.post("/v1/runs", json={
                        "upload_ids": [attachment["id"]], "worker_id": WORKER_ID, "approval_mode": "allow",
                    })).raise_for_status().json()
                    events_url = f"/v1/runs/{run['id']}/events"

                    async def reasoning_started():
                        events = (await client.get(events_url)).raise_for_status().json()
                        return events if any(event["type"] == "assistant_delta" for event in events) else None

                    try:
                        partial = await eventually(reasoning_started, timeout=10)
                        self.assertFalse(any(event["type"] == "assistant" for event in partial))
                        progress = next(event["data"] for event in partial if event["type"] == "assistant_delta")
                        self.assertEqual(progress["reasoning_content"], reasoning[:len(reasoning) // 2])
                        running = (await client.get(f"/v1/runs/{run['id']}")).raise_for_status().json()
                        self.assertEqual(running["status"], "running")
                    finally:
                        continue_after_reasoning.set()
                    finished = await wait_for_run(client, run["id"])
                    self.assertEqual(finished["status"], "completed", finished["error"])
                    self.assertEqual(len(provider_calls), 2)
                    files = finished["files"]
                    self.assertEqual([file["source"] for file in files], ["upload", "preview", "preview", "output"])
                    self.assertEqual(finished["output"], "Visual verification finished.")
                    display = (await client.get(f"/v1/sessions/{run['session_id']}/runs")).raise_for_status().json()
                    self.assertEqual(display[0]["files"], files)
                    for file in (files[0], files[-1]):
                        response = (await client.get(f"/v1/runs/{run['id']}/files/{file['id']}")).raise_for_status()
                        self.assertEqual(response.content, original)
                        self.assertTrue(response.headers["content-disposition"].startswith("attachment;"))
                    response = (await client.get(events_url)).raise_for_status()
                    self.assertIn("artifact_id", response.text)
                    self.assertNotIn("base64", response.text)
                    answers = [event["data"] for event in response.json() if event["type"] == "assistant"]
                    self.assertEqual(answers[0]["message"]["tool_calls"], provider_calls[0]["tool_calls"])
                    self.assertEqual(answers[0]["message"]["reasoning_content"], reasoning)
                    self.assertEqual(answers[0]["usage"], {"total_tokens": 3})
                    self.assertEqual(answers[0]["generation_id"], progress["generation_id"])
                    self.assertNotEqual(answers[0]["generation_id"], answers[1]["generation_id"])
                    reused = await client.post("/v1/runs", json={"upload_ids": [attachment["id"]]})
                    self.assertEqual(reused.status_code, 409)
