import asyncio
import os
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
from PIL import Image

from harness.api.server import create_app
from harness.config import Settings
from harness.execution.protocol import ARTIFACT_CHUNK_BYTES, decode
from harness.execution.router import WorkerConnection, WorkerManager, WorkerUnavailable
from tests.harness_support import wait_for_run
from workers.common.transport import _Runtime
from workers.desktop.worker import WorkerBackend


class WorkerRoutingTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.manager = WorkerManager("test-worker-secret")

    def connect(self, worker_id, environment, *, computer=True):
        capabilities = {"environment": environment, "computer": computer}
        worker = SimpleNamespace(worker_id=worker_id, capabilities=capabilities, request=AsyncMock(return_value={}))
        self.manager._known_workers[worker_id] = capabilities
        self.manager._workers[worker_id] = worker
        return worker

    async def test_automatic_routing_prefers_idle_desktops(self):
        browser = self.connect("cloud", "cloud-browser")
        windows = self.connect("windows", "windows-desktop")
        mac = self.connect("mac", "macos-desktop")

        self.assertIs(await self.manager.acquire(None, "first", "ws-1"), mac)
        self.assertIs(await self.manager.acquire(None, "second", "ws-2"), windows)
        with self.assertRaisesRegex(WorkerUnavailable, "Desktop workers are busy or offline"):
            await self.manager.acquire(None, "third", "ws-3")
        browser.request.assert_not_awaited()

    async def test_offline_desktop_pauses_automatic_routing_but_allows_explicit_browser(self):
        browser = self.connect("cloud", "cloud-browser")
        desktop = self.connect("desktop", "macos-desktop")
        self.manager._workers.pop(desktop.worker_id)

        with self.assertRaisesRegex(WorkerUnavailable, "Desktop workers are busy or offline"):
            await self.manager.acquire(None, "run-1", "ws-1")
        browser.request.assert_not_awaited()
        self.assertIs(await self.manager.acquire(browser.worker_id, "run-1", "ws-1"), browser)

    async def test_headless_server_uses_browser(self):
        browser = self.connect("cloud", "cloud-browser")
        disabled = self.connect("disabled-desktop", "windows-desktop", computer=False)

        self.assertIs(await self.manager.acquire(None, "run-1", "ws-1"), browser)
        disabled.request.assert_not_awaited()

    async def test_targeted_page_runs_can_generate_concurrently(self):
        self.connect("desktop", "macos-desktop")
        self.connect("cloud", "cloud-browser")
        self.connect("cloud-2", "cloud-browser")
        started = {task: asyncio.Event() for task in ("first", "second", "third")}
        finish = asyncio.Event()

        async def complete(messages, tools):
            task = messages[-1]["content"]
            started[task].set()
            await finish.wait()
            return {"content": f"Reply to {task}"}

        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(data_dir=Path(directory), api_token="test-token")
            app = create_app(settings, worker_manager=self.manager, llm=SimpleNamespace(complete=complete))
            transport = httpx.ASGITransport(app=app)
            async with app.router.lifespan_context(app), httpx.AsyncClient(
                transport=transport, base_url="http://127.0.0.1",
                headers={"Authorization": "Bearer test-token"},
            ) as client:
                async def start(task, target):
                    response = await client.post("/v1/runs", json={"task": task, "worker_target": target})
                    return response.raise_for_status().json()

                async with asyncio.timeout(5):
                    first = await start("first", "auto")
                    await started["first"].wait()
                    desktop = await start("desktop only", "desktop")
                    paused = await wait_for_run(client, desktop["id"], "paused")
                    self.assertIsNone(paused["worker_id"])
                    self.assertEqual(paused["worker_target"], "desktop")

                    second, third = await asyncio.gather(start("second", "auto"), start("third", "browser"))
                    await asyncio.gather(started["second"].wait(), started["third"].wait())
                    self.assertEqual(len({run["session_id"] for run in (first, second, third)}), 3)
                    running = [app.state.store.get(run["id"]) for run in (first, second, third)]
                    self.assertTrue(all(run["status"] == "running" for run in running))
                    self.assertEqual(running[0]["worker_id"], "desktop")
                    self.assertEqual({run["worker_id"] for run in running[1:]}, {"cloud", "cloud-2"})

                    finish.set()
                    for run in running:
                        done = await wait_for_run(client, run["id"], "completed")
                        self.assertEqual(done["output"], f"Reply to {run['task']}")


class WorkerTransportTest(unittest.IsolatedAsyncioTestCase):
    async def test_duplicate_id_cannot_execute_twice(self):
        backend = AsyncMock()
        runtime = _Runtime(backend)
        await runtime.execute("open-1", "run.open", {"run_id": "run-1", "workspace_id": "ws-1"})
        arguments = {"run_id": "run-1", "command": "pwd", "timeout": 5}
        await runtime.execute("call-1", "bash.execute", arguments)
        with self.assertRaisesRegex(ValueError, "Call ID already handled"):
            await runtime.execute("call-1", "bash.execute", arguments)
        backend.bash.assert_awaited_once_with("run-1", "pwd", 5)

    async def test_resume_requires_the_retained_workspace(self):
        backend = AsyncMock()
        runtime = _Runtime(backend)
        with self.assertRaisesRegex(ValueError, "original workspace is unavailable"):
            await runtime.execute("resume-1", "run.resume", {"run_id": "run-1", "workspace_id": "ws-1"})
        backend.open_run.assert_not_awaited()
        await runtime.execute("open-1", "run.open", {"run_id": "run-1", "workspace_id": "ws-1"})
        with self.assertRaisesRegex(ValueError, "original workspace is unavailable"):
            await runtime.execute("resume-2", "run.resume", {"run_id": "run-1", "workspace_id": "ws-2"})
        await runtime.execute("resume-3", "run.resume", {"run_id": "run-1", "workspace_id": "ws-1"})
        backend.open_run.assert_awaited_once_with("run-1", "ws-1")

    @unittest.skipIf(os.name == "nt", "Uses the POSIX desktop Bash executable")
    async def test_bash_artifacts_round_trip_over_worker_frames(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            backend = WorkerBackend(root)
            backend.bash_enabled = True
            backend.check_available = lambda: None
            runtime = _Runtime(backend)
            manager = WorkerManager("test-worker-secret")

            async def request(call_id, method, params, timeout):
                return await asyncio.wait_for(runtime.execute(call_id, method, params), timeout)

            worker = SimpleNamespace(worker_id="desktop", capabilities=backend.capabilities(),
                                     request=request)
            manager._workers[worker.worker_id] = worker
            manager._known_workers[worker.worker_id] = worker.capabilities
            await manager.acquire(worker.worker_id, "run-1", "workspace-1")
            help_result = await manager.bash(
                worker.worker_id, "run-1", "help",
                "harness view --help; harness register --help; harness computer --help", 5,
            )
            self.assertIn("--crop", help_result["stdout"])
            self.assertIn("harness register FILE", help_result["stdout"])
            self.assertIn("Usage: harness computer", help_result["stdout"])
            blocked = await manager.bash(
                worker.worker_id, "run-1", "blocked", "true && harness computer click 5 5", 5,
            )
            self.assertEqual(blocked["exit_code"], 2)
            self.assertIn("GUI approval", blocked["stderr"])

            raster = BytesIO()
            Image.new("RGB", (24, 30), "red").save(raster, "PNG")
            original = b"z" * (ARTIFACT_CHUNK_BYTES + 1)
            await manager.stage_artifact(worker.worker_id, "run-1", "uploads/input image.png", raster.getvalue())
            await manager.stage_artifact(worker.worker_id, "run-1", "uploads/original.bin", original)
            await manager.stage_artifact(worker.worker_id, "run-1", "uploads/empty.bin", b"")
            workspace = backend._runs["run-1"].path
            private = root / "private.txt"
            private.write_text("This data belongs outside the workspace.")
            (workspace / "outside").symlink_to(private)
            (workspace / "linked").hardlink_to(private)

            outgoing = AsyncMock()
            await runtime.respond(outgoing, asyncio.Lock(), "call-1", "bash.execute", {
                "run_id": "run-1", "timeout": 5,
                "command": "cd uploads; harness view --crop 0,1,10,12 'input image.png'; "
                           "harness register original.bin empty.bin ../outside ../linked; printf done",
            })
            metadata, *payloads = [call.args[0] for call in outgoing.send.await_args_list]
            connection = WorkerConnection(worker.worker_id, {}, AsyncMock())
            connection.websocket.receive.side_effect = [
                {"type": "websocket.receive", "bytes": data} for data in payloads
            ]
            pending = asyncio.get_running_loop().create_future()
            connection.pending["call-1"] = pending
            await manager._receive_result(connection, decode(metadata))
            result = pending.result()
            self.assertEqual(result["stdout"], "done")
            self.assertEqual(result["stderr"], "")
            self.assertEqual(result["exit_code"], 0)
            label, image, artifact, empty, *denied = result["content"]
            self.assertIn("24 × 30", label["text"])
            self.assertEqual(image["type"], "image")
            self.assertEqual(image["source_path"], "uploads/input image.png")
            with Image.open(BytesIO(image["data"])) as cropped:
                self.assertEqual(cropped.size, (10, 12))
            self.assertEqual(artifact["path"], "uploads/original.bin")
            self.assertEqual(artifact["data"], original)
            self.assertEqual(empty["data"], b"")
            self.assertEqual([block["type"] for block in denied], ["text", "text"])
            self.assertNotIn(private.read_text(), str(denied))
            await runtime.close()

    async def test_computer_cli_supports_desktop_without_bash(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            backend = WorkerBackend(root)
            backend.desktop_enabled = True
            backend.bash_enabled = False
            backend.check_available = Mock()
            gui = Mock()
            gui.size.return_value = (100, 80)
            gui.onScreen.return_value = True
            gui.FailSafeException = type("FailSafeException", (Exception,), {})
            backend._get_gui = Mock(return_value=gui)
            raster = BytesIO()
            Image.new("RGB", (100, 80), "white").save(raster, "PNG")
            backend._screenshot = Mock(return_value={
                "image": raster.getvalue(), "mime_type": "image/png", "width": 100, "height": 80,
            })
            runtime = _Runtime(backend)
            manager = WorkerManager("test-worker-secret")

            async def request(call_id, method, params, timeout):
                return await asyncio.wait_for(runtime.execute(call_id, method, params), timeout)

            worker = SimpleNamespace(worker_id="desktop", capabilities=backend.capabilities(),
                                     request=AsyncMock(side_effect=request))
            manager._workers[worker.worker_id] = worker
            manager._known_workers[worker.worker_id] = worker.capabilities
            self.assertTrue(worker.capabilities["computer_cli"])
            await manager.acquire(worker.worker_id, "run-1", "workspace-1")
            info = await manager.computer_cli(
                worker.worker_id, "run-1", "info", {"action": "screen_info"}, observe=False, output=None, timeout=7,
            )
            self.assertEqual(info, {"target": "desktop", "width": 100, "height": 80})
            self.assertEqual(worker.request.await_args.args[-1], 7)
            backend._screenshot.assert_not_called()

            capture = await manager.computer_cli(
                worker.worker_id, "run-1", "capture", {"action": "screenshot"}, observe=True, output="shots/ui.jpg",
            )
            backend._screenshot.assert_called_once_with(gui)
            self.assertEqual(capture["image"], raster.getvalue())
            self.assertEqual(capture["path"], "shots/ui.jpg")
            self.assertEqual(capture["output_mime_type"], "image/jpeg")
            workspace = backend._runs["run-1"].path
            with Image.open(workspace / capture["path"]) as saved:
                self.assertEqual((saved.format, saved.size), ("JPEG", (100, 80)))
            if os.name != "nt":
                private = root / "private.png"
                private.write_bytes(b"private")
                (workspace / "linked.png").symlink_to(private)
                with self.assertRaisesRegex(ValueError, "regular workspace file"):
                    await manager.computer_cli(
                        worker.worker_id, "run-1", "linked", {"action": "screenshot"},
                        observe=False, output="linked.png",
                    )
                self.assertEqual(private.read_bytes(), b"private")

            backend._screenshot.side_effect = RuntimeError("capture failed")
            moved = await manager.computer_cli(
                worker.worker_id, "run-1", "move", {"action": "move", "x": 5, "y": 10},
                observe=True, output=None,
            )
            gui.moveTo.assert_called_once_with(5, 10)
            self.assertEqual(moved, {"ok": True, "observation_error": "capture failed"})
            await runtime.close()
