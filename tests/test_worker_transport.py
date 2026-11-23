import unittest
from unittest.mock import AsyncMock

from workers.common.transport import _Runtime


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
