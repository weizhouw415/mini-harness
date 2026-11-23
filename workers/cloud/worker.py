import asyncio
import contextlib
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from workers.common.browser import BROWSER_ACTIONS, BrowserController, BrowserSession
from workers.common.config import env_flag
from workers.common.shell import run_shell


@dataclass
class Run:
    path: Path
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    browser_session: BrowserSession = field(default_factory=BrowserSession)
    closed: bool = False


class WorkerBackend:
    def __init__(self, workspace_root: Path | None = None, executable_path: str | None = None):
        self.workspace_root = Path(
            workspace_root or os.getenv("MINI_WORKER_WORKSPACE_ROOT") or "/tmp/mini-harness-worker"
        ).expanduser().resolve()
        self._runs: dict[str, Run] = {}
        self._browser = BrowserController(
            executable_path or os.getenv("MINI_CLOUD_BROWSER_EXECUTABLE"),
            chromium_sandbox=env_flag("MINI_CLOUD_CHROMIUM_SANDBOX"),
        )

    def capabilities(self) -> dict:
        return {
            "environment": "cloud-browser", "desktop": False, "bash": True,
            "computer": True, "download": True, "upload": True, "actions": list(BROWSER_ACTIONS),
            "computer_targets": {"browser": list(BROWSER_ACTIONS)},
        }

    async def open_run(self, run_id: str, workspace_id: str) -> None:
        if run_id in self._runs and not self._runs[run_id].closed:
            return
        if self._runs:
            raise RuntimeError("This cloud worker already has an active run")
        self.workspace_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = Path(tempfile.mkdtemp(prefix="run-", dir=self.workspace_root))
        self._runs[run_id] = Run(path)

    def _get_run(self, run_id: str) -> Run:
        try:
            return self._runs[run_id]
        except KeyError:
            raise ValueError("Unknown run") from None

    async def bash(self, run_id: str, command: str, timeout: float) -> dict:
        run = self._get_run(run_id)
        async with run.lock:
            if run.closed:
                raise ValueError("Run is closed")
            return await run_shell(
                ["/bin/bash", "--noprofile", "--norc", "-c", command],
                cwd=run.path, timeout=timeout,
                environment={
                    "PATH": "/usr/local/bin:/usr/bin:/bin",
                    "HOME": str(run.path), "TMPDIR": str(run.path), "LANG": "C.UTF-8",
                },
            )

    async def computer(self, run_id: str, action: dict) -> dict:
        run = self._get_run(run_id)
        async with run.lock:
            if run.closed:
                raise ValueError("Run is closed")
            return await self._browser.computer(run.browser_session, run.path, action)

    async def close_run(self, run_id: str) -> None:
        run = self._runs.get(run_id)
        if run is None:
            return
        run.closed = True
        async with run.lock:
            await self._browser.close_session(run.browser_session)
            with contextlib.suppress(FileNotFoundError):
                await asyncio.to_thread(shutil.rmtree, run.path)
            self._runs.pop(run_id, None)

    async def close(self) -> None:
        try:
            for run_id in list(self._runs):
                await self.close_run(run_id)
        finally:
            await self._browser.close()
