"""Run computer actions in a private headless Chromium context."""

from __future__ import annotations

import contextlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from workers.common.actions import GUI_ACTIONS, point_fields, scroll_deltas, text_field

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, Page, Playwright


BROWSER_ACTIONS = (*GUI_ACTIONS, "observe", "navigate", "press", "download", "upload")
VIEWPORT = {"width": 1280, "height": 720}
CLICK_OPTIONS = {"double_click": {"click_count": 2}, "right_click": {"button": "right"}}


@dataclass
class BrowserSession:
    context: BrowserContext | None = None
    page: Page | None = None


class BrowserController:
    def __init__(self, executable_path: str | None, *, chromium_sandbox: bool):
        self.executable_path = executable_path
        self.chromium_sandbox = chromium_sandbox
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None

    async def _get_page(self, session: BrowserSession) -> Page:
        if self._browser is None or not self._browser.is_connected():
            from playwright.async_api import async_playwright

            if self._playwright is None:
                self._playwright = await async_playwright().start()
            environment = {
                name: value for name, value in os.environ.items()
                if not name.upper().startswith(("HARNESS_", "MINI_"))
            }
            self._browser = await self._playwright.chromium.launch(
                headless=True, chromium_sandbox=self.chromium_sandbox,
                executable_path=self.executable_path, env=environment,
            )
        if session.context is None or session.context.browser is not self._browser:
            session.page = None
            session.context = await self._browser.new_context(
                viewport=VIEWPORT, accept_downloads=True, device_scale_factor=1,
            )
            session.context.set_default_timeout(15_000)
            session.context.set_default_navigation_timeout(25_000)
        if session.page is None or session.page.is_closed():
            session.page = await session.context.new_page()
        return session.page

    @staticmethod
    def _workspace_path(workspace: Path, path: str) -> Path:
        resolved = (workspace / path).resolve()
        if resolved == workspace or not resolved.is_relative_to(workspace):
            raise ValueError("File path must be inside the run workspace")
        return resolved

    @staticmethod
    async def _click(page: Page, action: dict) -> None:
        options = CLICK_OPTIONS.get(action["action"], {})
        if "selector" in action:
            await page.locator(text_field(action, "selector")).click(**options)
        else:
            await page.mouse.click(*point_fields(action), **options)

    @staticmethod
    async def _drag(page: Page, action: dict) -> None:
        start = point_fields(action)
        end = point_fields(action, "end_x", "end_y")
        await page.mouse.move(*start)
        await page.mouse.down()
        try:
            await page.mouse.move(*end, steps=10)
        finally:
            await page.mouse.up()

    async def _download(self, workspace: Path, page: Page, action: dict) -> dict:
        destination = self._workspace_path(workspace, text_field(action, "path")) if "path" in action else None
        async with page.expect_download() as download_event:
            await self._click(page, action)
        download = await download_event.value
        if destination is None:
            destination = self._workspace_path(workspace, Path(download.suggested_filename).name)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(prefix=".download-", dir=destination.parent, delete=False) as file:
            temporary = Path(file.name)
        try:
            await download.save_as(temporary)
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)
        return {"path": destination.relative_to(workspace).as_posix(), "size": destination.stat().st_size}

    async def computer(self, session: BrowserSession, workspace: Path, action: dict) -> dict:
        if action.get("target", "browser") != "browser":
            raise ValueError("This browser only accepts browser computer actions")
        name = text_field(action, "action", limit=40)
        if name not in BROWSER_ACTIONS:
            raise ValueError(f"Unsupported browser action: {name}")
        page = await self._get_page(session)
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
        if name in {"click", "double_click", "right_click"}:
            await self._click(page, action)
        elif name == "move":
            await page.mouse.move(*point_fields(action))
        elif name == "drag":
            await self._drag(page, action)
        elif name == "type":
            value = text_field(action, "text", allow_empty=True, limit=100_000)
            if "selector" in action:
                await page.locator(text_field(action, "selector")).fill(value)
            else:
                await page.keyboard.insert_text(value)
        elif name == "scroll":
            dx, dy = scroll_deltas(action)
            if "x" in action or "y" in action:
                await page.mouse.move(*point_fields(action))
            await page.mouse.wheel(dx, dy)
        elif name in {"key", "press"}:
            key = text_field(action, "key", limit=80)
            if "selector" in action:
                await page.locator(text_field(action, "selector")).press(key)
            else:
                await page.keyboard.press(key)
        elif name == "download":
            return await self._download(workspace, page, action)
        elif name == "upload":
            path = self._workspace_path(workspace, text_field(action, "path"))
            if not path.is_file():
                raise ValueError("Upload source must be a file in the run workspace")
            await page.locator(text_field(action, "selector")).set_input_files(path)
            return {"path": path.relative_to(workspace).as_posix(), "size": path.stat().st_size}
        return {"ok": True}

    @staticmethod
    async def close_session(session: BrowserSession) -> None:
        context, session.context = session.context, None
        session.page = None
        if context is not None:
            with contextlib.suppress(Exception):
                await context.close()

    async def close(self) -> None:
        try:
            browser, self._browser = self._browser, None
            if browser is not None:
                await browser.close()
        finally:
            playwright, self._playwright = self._playwright, None
            if playwright is not None:
                await playwright.stop()
