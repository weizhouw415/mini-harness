"""Start the local server and its browser and desktop workers."""
from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path


def main():
    config = Path(".harness/local.env")
    env = os.environ.copy()
    if config.exists():
        for line in config.read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition("=")
            if separator and key in {"HARNESS_API_TOKEN", "HARNESS_WORKER_SECRET"}:
                env.setdefault(key, value)
    if not env.get("HARNESS_API_TOKEN") or not env.get("HARNESS_WORKER_SECRET"):
        raise SystemExit("Set HARNESS_API_TOKEN and HARNESS_WORKER_SECRET, or add them to .harness/local.env.")
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", 8000))
        except OSError as error:
            raise SystemExit("Port 8000 is unavailable. Stop the existing local server first.") from error

    browser_env = env.copy()
    browser_env["MINI_WORKER_SECRET"] = env["HARNESS_WORKER_SECRET"]
    browser_env["MINI_WORKER_ID"] = "cloud"
    if sys.platform == "darwin" and not browser_env.get("MINI_CLOUD_BROWSER_EXECUTABLE"):
        chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
        if chrome.is_file():
            browser_env["MINI_CLOUD_BROWSER_EXECUTABLE"] = str(chrome)

    desktop_env = env.copy()
    desktop_env.update({
        "MINI_WORKER_SECRET": env["HARNESS_WORKER_SECRET"],
        "MINI_WORKER_ID": "desktop",
        "MINI_DESKTOP_CONTROL": "1",
    })
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
        children.append(subprocess.Popen([sys.executable, "-m", "workers.cloud"], env=browser_env))
        if sys.platform in {"darwin", "win32"}:
            children.append(subprocess.Popen([sys.executable, "-m", "workers.desktop"], env=desktop_env))
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
