import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from harness.execution.router import WorkerManager, WorkerUnavailable
from workers.common.transport import _Runtime


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
