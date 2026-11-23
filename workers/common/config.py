import argparse
import os


def env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def worker_arguments(description: str, default_id: str, *, pause_controls: bool = False) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--url", default=os.getenv("MINI_WORKER_URL", "ws://127.0.0.1:8000/v1/workers/ws"))
    parser.add_argument("--worker-id", default=os.getenv("MINI_WORKER_ID", default_id))
    parser.add_argument("--allow-insecure", action="store_true", default=env_flag("MINI_WORKER_ALLOW_INSECURE"))
    if pause_controls:
        controls = parser.add_mutually_exclusive_group()
        controls.add_argument("--pause", action="store_true", help="Pause this user's desktop worker")
        controls.add_argument("--resume", action="store_true", help="Resume this user's desktop worker")
    arguments = parser.parse_args()
    if pause_controls and (arguments.pause or arguments.resume):
        return arguments
    arguments.secret = os.environ.pop("MINI_WORKER_SECRET", "")
    if not arguments.secret:
        parser.error("set MINI_WORKER_SECRET to the worker's shared secret")
    return arguments
