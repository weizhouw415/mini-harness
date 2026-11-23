import asyncio
import signal
import sys

from workers.common.config import worker_arguments
from workers.common.transport import serve_worker
from workers.desktop.worker import WorkerBackend


async def main() -> None:
    options = worker_arguments("Run the locally authorized desktop worker", "desktop", pause_controls=True)
    worker = WorkerBackend()
    if options.pause:
        worker.pause()
        print("Desktop worker paused.")
        return
    if options.resume:
        worker.resume()
        print("Desktop worker resumed.")
        return
    try:
        worker.check_available()
    except (RuntimeError, PermissionError) as exc:
        raise SystemExit(str(exc)) from exc
    loop = asyncio.get_running_loop()
    task = asyncio.create_task(serve_worker(
        worker, url=options.url, worker_id=options.worker_id, secret=options.secret,
        allow_insecure=options.allow_insecure,
    ))
    if sys.platform != "win32":
        loop.add_signal_handler(signal.SIGTERM, task.cancel)
    try:
        await task
    except asyncio.CancelledError:
        pass
    finally:
        await worker.close()


if __name__ == "__main__":
    asyncio.run(main())
