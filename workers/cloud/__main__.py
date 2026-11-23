import asyncio
import ctypes
import signal
import sys

from workers.cloud.worker import WorkerBackend
from workers.common.config import worker_arguments
from workers.common.transport import serve_worker


async def main() -> None:
    options = worker_arguments("Run the cloud browser worker", "cloud")
    if sys.platform == "linux":
        # Worker commands share the container UID. Protect the process environment
        # containing the launch credentials from reads through procfs.
        if ctypes.CDLL(None, use_errno=True).prctl(4, 0, 0, 0, 0) != 0:
            raise RuntimeError("Unable to protect the worker process")
    worker = WorkerBackend()
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
