"""Apply CLI options around a worker's existing computer actions."""

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path

from workers.common.artifacts import save_screenshot, screenshot_output_path


async def run_computer_cli(
    workspace: Path, action: dict, execute: Callable[[dict], Awaitable[dict]], *,
    observe: bool, output: str | None,
) -> dict:
    parts = None
    if output is not None:
        if action.get("action") != "screenshot":
            raise ValueError("Only screenshot supports saving a capture to a workspace output path.")
        parts = screenshot_output_path(output)
    result = await execute(action)
    if observe and "image" not in result:
        capture = {"action": "observe"}
        if "target" in action:
            capture["target"] = action["target"]
        try:
            result.update(await execute(capture))
        except Exception as error:
            # Preserve the action's outcome if a separate observation fails.
            result["observation_error"] = str(error)[:1000] or type(error).__name__
    if parts is not None:
        task = asyncio.create_task(asyncio.to_thread(
            save_screenshot, workspace, parts, result.get("image"), result.get("mime_type"),
        ))
        try:
            result.update(await asyncio.shield(task))
        except asyncio.CancelledError:
            await asyncio.gather(task, return_exceptions=True)
            raise
    return result
