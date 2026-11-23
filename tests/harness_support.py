import asyncio
import json
from inspect import isawaitable


def tool_call(call_id: str, name: str, arguments: dict):
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }
        ],
    }


async def eventually(check, *, timeout: float = 20.0):
    async with asyncio.timeout(timeout):
        while True:
            result = check()
            if isawaitable(result):
                result = await result
            if result:
                return result
            await asyncio.sleep(0.05)


async def wait_for_run(client, run_id, *statuses):
    stops = {*statuses, "completed", "failed", "cancelled"}

    async def run_in_status():
        data = (await client.get(f"/v1/runs/{run_id}")).raise_for_status().json()
        return data if data["status"] in stops else None

    return await eventually(run_in_status, timeout=45.0)
