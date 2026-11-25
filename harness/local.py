"""Start the local server and its desktop and cloud browser workers."""
from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def _chrome_executable(env: dict[str, str]) -> str | None:
    if sys.platform == "darwin":
        candidates = [Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")]
    elif sys.platform == "win32":
        candidates = [Path(env[key]) / "Google" / "Chrome" / "Application" / "chrome.exe"
                      for key in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA") if env.get(key)]
    else:
        candidates = []
    return next((str(path) for path in candidates if path.is_file()), None)


def main(*, headless: bool = False):
    config = Path(".harness/local.env")
    env = os.environ.copy()
    if config.exists():
        for line in config.read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition("=")
            if separator and key in {"HARNESS_API_TOKEN", "HARNESS_WORKER_SECRET", "MINI_DESKTOP_BASH"}:
                env.setdefault(key, value)
    if not env.get("HARNESS_API_TOKEN") or not env.get("HARNESS_WORKER_SECRET"):
        raise SystemExit("Set HARNESS_API_TOKEN and HARNESS_WORKER_SECRET, or add them to .harness/local.env.")
    with socket.socket() as probe:
        if os.name == "posix":
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", 8000))
        except OSError as error:
            raise SystemExit("Port 8000 is unavailable. Stop the existing local server first.") from error

    desktop = not headless and sys.platform in {"darwin", "win32"}
    worker_env = env.copy()
    worker_env["MINI_WORKER_SKILLS_DIR"] = str(Path(
        env.get("HARNESS_SKILLS_DIR") or Path(env.get("HARNESS_DATA_DIR", ".harness")) / "skills"
    ).expanduser().resolve())
    worker_env["MINI_WORKER_SECRET"] = worker_env.pop("HARNESS_WORKER_SECRET")
    worker_env.pop("HARNESS_API_TOKEN", None)
    browser_env = worker_env.copy()
    if not browser_env.get("MINI_CLOUD_BROWSER_EXECUTABLE"):
        if chrome := _chrome_executable(browser_env):
            browser_env["MINI_CLOUD_BROWSER_EXECUTABLE"] = chrome
    if desktop:
        browser_env.setdefault("MINI_CLOUD_CHROMIUM_SANDBOX", "1")
    if sys.platform == "win32":
        browser_env.setdefault("MINI_WORKER_WORKSPACE_ROOT", str(Path(tempfile.gettempdir()) / "mini-harness-worker"))

    children = []
    stopping = False

    def stop(_signum, _frame):
        nonlocal stopping
        stopping = True
        for child in children:
            if child.poll() is None:
                child.terminate()

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, stop)

    try:
        children.append(subprocess.Popen([sys.executable, "-m", "harness.cli", "serve"], env=env))
        if desktop:
            desktop_env = {**worker_env, "MINI_WORKER_ID": "desktop", "MINI_DESKTOP_CONTROL": "1"}
            children.append(subprocess.Popen([sys.executable, "-m", "workers.desktop"], env=desktop_env))
        for worker_id in ("cloud", "cloud-2", "cloud-3"):
            children.append(subprocess.Popen(
                [sys.executable, "-m", "workers.cloud"], env={**browser_env, "MINI_WORKER_ID": worker_id},
            ))
        print("Mini Harness is starting at http://127.0.0.1:8000/ (Ctrl+C to stop)", flush=True)
        while not stopping:
            for child in children:
                if child.poll() is not None:
                    print(f"Process {child.pid} exited with status {child.returncode}.", file=sys.stderr)
                    return child.returncode or 1
            time.sleep(0.5)
        return 0
    finally:
        stop(None, None)
        for child in children:
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()


if __name__ == "__main__":
    raise SystemExit(main())
