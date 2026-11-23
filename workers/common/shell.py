import asyncio
import contextlib
import os
import signal
import subprocess
from pathlib import Path


OUTPUT_LIMIT = 64 * 1024


async def _read_output(stream: asyncio.StreamReader) -> tuple[str, bool]:
    collected = bytearray()
    truncated = False
    while chunk := await stream.read(16 * 1024):
        available = OUTPUT_LIMIT - len(collected)
        collected.extend(chunk[:available])
        truncated |= len(chunk) > available
    return collected.decode("utf-8", errors="replace"), truncated


async def _stop_process(process: asyncio.subprocess.Process) -> None:
    if os.name == "posix":
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
    elif process.returncode is None:
        with contextlib.suppress(OSError, TimeoutError):
            stop = await asyncio.create_subprocess_exec(
                "taskkill.exe", "/T", "/F", "/PID", str(process.pid),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            await asyncio.wait_for(stop.wait(), timeout=5)
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()


async def run_shell(
    arguments: list[str],
    *,
    cwd: Path,
    environment: dict[str, str],
    timeout: float,
) -> dict:
    process_options = (
        {"start_new_session": True} if os.name == "posix"
        else {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    )
    process = await asyncio.create_subprocess_exec(
        *arguments, cwd=cwd, env=environment, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, **process_options,
    )
    stdout_task = asyncio.create_task(_read_output(process.stdout))
    stderr_task = asyncio.create_task(_read_output(process.stderr))
    done = asyncio.gather(process.wait(), stdout_task, stderr_task)
    try:
        try:
            exit_code, stdout, stderr = await asyncio.wait_for(asyncio.shield(done), timeout)
            timed_out = False
        except TimeoutError:
            timed_out = True
            await _stop_process(process)
            _, stdout, stderr = await asyncio.wait_for(asyncio.shield(done), 5)
            exit_code = None
    finally:
        await _stop_process(process)
        if not done.done():
            done.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await done
    return {
        "stdout": stdout[0], "stderr": stderr[0], "exit_code": exit_code,
        "timed_out": timed_out, "truncated": stdout[1] or stderr[1],
    }
