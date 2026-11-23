import asyncio
import contextlib
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from workers.common.actions import integer_field, text_field
from workers.common.config import env_flag
from workers.common.shell import run_shell


ACTIONS = ("screenshot", "observe", "navigate", "click", "type", "scroll", "press", "download", "upload")
VIEWPORT = {"width": 1280, "height": 720}


@dataclass
class Run:
    path: Path
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    context: object = None
    page: object = None
    closed: bool = False


class WorkerBackend:
    def __init__(self, workspace_root: Path | None = None, executable_path: str | None = None):
        self.workspace_root = Path(
            workspace_root or os.getenv("MINI_WORKER_WORKSPACE_ROOT") or "/tmp/mini-harness-worker"
        ).expanduser().resolve()
        self.executable_path = executable_path or os.getenv("MINI_CLOUD_BROWSER_EXECUTABLE")
        self._runs: dict[str, Run] = {}
        self._playwright = None
        self._browser = None

    def capabilities(self) -> dict:
        return {
            "environment": "cloud-browser", "desktop": False, "bash": True,
            "computer": True, "download": True, "upload": True, "actions": list(ACTIONS),
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

    async def _get_page(self, run: Run):
        if self._browser is None or not self._browser.is_connected():
            from playwright.async_api import async_playwright

            run.context = run.page = None
            if self._playwright is None:
                self._playwright = await async_playwright().start()
            launch = {"headless": True, "chromium_sandbox": env_flag("MINI_CLOUD_CHROMIUM_SANDBOX")}
            if self.executable_path:
                launch["executable_path"] = self.executable_path
            self._browser = await self._playwright.chromium.launch(**launch)
        if run.context is None:
            run.context = await self._browser.new_context(
                viewport=VIEWPORT, accept_downloads=True, device_scale_factor=1,
            )
            run.context.set_default_timeout(15_000)
            run.context.set_default_navigation_timeout(25_000)
        if run.page is None or run.page.is_closed():
            run.page = await run.context.new_page()
        return run.page

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

    @staticmethod
    def _workspace_path(run: Run, path: str) -> Path:
        resolved = (run.path / path).resolve()
        if resolved == run.path or not resolved.is_relative_to(run.path):
            raise ValueError("File path must be inside the run workspace")
        return resolved

    @staticmethod
    async def _click(page, action: dict) -> None:
        if "selector" in action:
            await page.locator(text_field(action, "selector")).click()
        else:
            await page.mouse.click(integer_field(action, "x"), integer_field(action, "y"))

    async def _download(self, run: Run, page, action: dict) -> dict:
        destination = self._workspace_path(run, text_field(action, "path")) if "path" in action else None
        async with page.expect_download() as download_event:
            await self._click(page, action)
        download = await download_event.value
        if destination is None:
            destination = self._workspace_path(run, Path(download.suggested_filename).name)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(prefix=".download-", dir=destination.parent, delete=False) as file:
            temporary = Path(file.name)
        try:
            await download.save_as(temporary)
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)
        return {"path": destination.relative_to(run.path).as_posix(), "size": destination.stat().st_size}

    async def computer(self, run_id: str, action: dict) -> dict:
        name = text_field(action, "action", limit=40)
        if name not in ACTIONS:
            raise ValueError(f"Unsupported browser action: {name}")
        run = self._get_run(run_id)
        async with run.lock:
            if run.closed:
                raise ValueError("Run is closed")
            page = await self._get_page(run)
            if name in {"screenshot", "observe"}:
                return {
                    "image": await page.screenshot(type="png", timeout=15_000),
                    "mime_type": "image/png", **VIEWPORT,
                    "url": page.url, "title": await page.title(),
                }
            if name == "navigate":
                address = text_field(action, "url")
                url = urlsplit(address)
                if url.scheme not in {"http", "https"} or not url.hostname:
                    raise ValueError("URL must use HTTP or HTTPS")
                response = await page.goto(address, wait_until="domcontentloaded")
                return {"url": page.url, "status": response.status if response else None}
            if name == "click":
                await self._click(page, action)
            elif name == "type":
                value = text_field(action, "text", allow_empty=True, limit=100_000)
                if "selector" in action:
                    await page.locator(text_field(action, "selector")).fill(value)
                else:
                    await page.keyboard.insert_text(value)
            elif name == "scroll":
                if "x" in action or "y" in action:
                    await page.mouse.move(integer_field(action, "x"), integer_field(action, "y"))
                await page.mouse.wheel(integer_field(action, "dx", default=0), integer_field(action, "dy", default=0))
            elif name == "press":
                key = text_field(action, "key", limit=80)
                if "selector" in action:
                    await page.locator(text_field(action, "selector")).press(key)
                else:
                    await page.keyboard.press(key)
            elif name == "download":
                return await self._download(run, page, action)
            elif name == "upload":
                path = self._workspace_path(run, text_field(action, "path"))
                if not path.is_file():
                    raise ValueError("Upload source must be a file in the run workspace")
                await page.locator(text_field(action, "selector")).set_input_files(path)
                return {"path": path.relative_to(run.path).as_posix(), "size": path.stat().st_size}
            return {"ok": True}

    async def close_run(self, run_id: str) -> None:
        run = self._runs.get(run_id)
        if run is None:
            return
        run.closed = True
        async with run.lock:
            if run.context is not None:
                with contextlib.suppress(Exception):
                    await run.context.close()
            with contextlib.suppress(FileNotFoundError):
                await asyncio.to_thread(shutil.rmtree, run.path)
            self._runs.pop(run_id, None)

    async def close(self) -> None:
        try:
            for run_id in list(self._runs):
                await self.close_run(run_id)
        finally:
            try:
                browser, self._browser = self._browser, None
                if browser is not None:
                    await browser.close()
            finally:
                playwright, self._playwright = self._playwright, None
                if playwright is not None:
                    await playwright.stop()
