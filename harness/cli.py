from __future__ import annotations

import argparse
import json
import os
import sys
import time
from urllib.parse import quote

import httpx


def main():
    parser = argparse.ArgumentParser(prog="mini-harness")
    parser.add_argument("--url", default=os.environ.get("HARNESS_URL", "http://127.0.0.1:8000"))
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    run = commands.add_parser("run")
    run.add_argument("task")
    run.add_argument("--session")
    run.add_argument("--worker")
    run.add_argument("--approval-mode", choices=("auto", "manual", "allow"), default="auto",
                     help="Tool approval policy (default: auto)")
    run.add_argument("--wait", action="store_true")
    local = commands.add_parser("local", help="Start the local server and workers")
    local.add_argument("--headless", action="store_true", help="Start only the cloud browser worker")
    commands.add_parser("workers")
    index = commands.add_parser("skills")
    index.add_argument("--environment")
    for verb in ("status", "trace", "resume", "cancel", "learn"):
        commands.add_parser(verb).add_argument("run_id")
    approval = commands.add_parser("approve")
    approval.add_argument("run_id")
    approval.add_argument("call_id")
    approval.add_argument("decision", choices=["allow", "deny"])
    for verb in ("evaluate", "publish", "rollback"):
        sub = commands.add_parser(verb)
        sub.add_argument("name")
        sub.add_argument("version")
        if verb == "evaluate":
            sub.add_argument("environment", choices=["cloud-browser", "macos-desktop", "windows-desktop"])
        else:
            sub.add_argument("--yes", required=True, action="store_true")

    args = parser.parse_args()
    if args.command == "local":
        from harness.local import main as local_main

        raise SystemExit(local_main(headless=args.headless))

    if args.command == "serve":
        import uvicorn
        from harness.api.server import create_app

        local_ui = args.host in {"127.0.0.1", "::1", "localhost"}
        uvicorn.run(lambda: create_app(local_ui=local_ui), factory=True,
                    host=args.host, port=args.port, proxy_headers=not local_ui)
        return

    token = os.environ.get("HARNESS_API_TOKEN")
    if not token:
        parser.error("Set HARNESS_API_TOKEN.")
    with httpx.Client(timeout=180, base_url=args.url, headers={"Authorization": f"Bearer {token}"}) as client:
        def send(method: str, path: str, **kwargs):
            response = client.request(method, path, **kwargs)
            if response.is_error:
                print(response.text, file=sys.stderr)
                raise SystemExit(1)
            return response.json()

        command = args.command
        try:
            if command == "run":
                result = send("POST", "/v1/runs", json={
                    "task": args.task, "worker_id": args.worker, "session_id": args.session,
                    "approval_mode": args.approval_mode,
                })
                if args.wait:
                    while result["status"] in {"queued", "running"}:
                        time.sleep(0.5)
                        result = send("GET", f"/v1/runs/{result['id']}")
            elif command == "workers":
                result = send("GET", "/v1/workers")
            elif command == "skills":
                params = {"environment": args.environment} if args.environment else {}
                result = send("GET", "/v1/skills", params=params)
            elif command in {"evaluate", "publish", "rollback"}:
                path = f"/v1/skills/{quote(args.name, safe='')}/{quote(args.version, safe='')}/{command}"
                body = {"environment": args.environment} if command == "evaluate" else {"approved": args.yes}
                result = send("POST", path, json=body)
            else:
                path = "/v1/runs/" + quote(args.run_id, safe="")
                if command == "status":
                    result = send("GET", path)
                elif command == "trace":
                    result = send("GET", path + "/events")
                elif command == "approve":
                    result = send("POST", path + "/approve", json={
                        "call_id": args.call_id, "approved": args.decision == "allow",
                    })
                else:
                    result = send("POST", path + "/" + command)
        except httpx.RequestError as error:
            print(f"API request failed: {error}", file=sys.stderr)
            raise SystemExit(1) from error
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.command == "run" and args.wait and result["status"] in {"failed", "cancelled"}:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
