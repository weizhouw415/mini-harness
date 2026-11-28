"""Build a compact Feishu card from a run's tool events."""

import re
from collections import Counter
from collections.abc import Iterable


_VISIBLE_CALLS = 5
_LINE_LIMIT = 120
_RUN_STATUS = {
    "queued": "排队中",
    "running": "进行中",
    "awaiting_approval": "等待审批",
    "paused": "已暂停",
    "completed": "已完成",
    "failed": "任务失败",
    "cancelled": "已取消",
}
_FINISHED = {"completed", "failed", "cancelled"}
_CALL_STATUS = {
    "success": ("✓", "成功"),
    "running": ("…", "执行中"),
    "failed": ("×", "未成功"),
    "unknown": ("?", "结果未知"),
}
_COMPUTER_ACTIONS = {
    "observe": "查看界面", "screenshot": "截取屏幕", "screen_info": "获取屏幕信息",
    "navigate": "打开网页", "click": "点击", "double_click": "双击", "right_click": "右击",
    "type": "输入文本", "scroll": "滚动", "press": "按键", "key": "按键",
    "move": "移动指针", "drag": "拖动", "upload": "上传文件", "download": "下载文件",
}
_SECRET_NAME = r"[\w-]*(?:password|passwd|secret|api[-_]?key|access[-_]?key|token)"
_INLINE_SECRET = re.compile(
    rf"((?:\bauthorization\s*:\s*)?\b(?:bearer|basic)\s+"
    rf"|\b{_SECRET_NAME}['\"]?\s*[:=]\s*"
    rf"|--{_SECRET_NAME}\s+)"
    r"""(?:"(?:\\.|[^"\\])*(?:"|$)|'[^']*(?:'|$)|(?:\\.|[^\s"'\\,;&|<>])+)""",
    re.IGNORECASE,
)
_URL_CREDENTIALS = re.compile(r"(?<=://)[^\s/@]+:[^\s/@]+@")
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f\u202a-\u202e\u2066-\u2069]")


def _one_line(value: str) -> str:
    lines = value.strip().splitlines()
    if not lines:
        return ""
    line = _CONTROL_CHARACTERS.sub("", lines[0]).replace("\t", " ").strip()
    line = _INLINE_SECRET.sub(r"\1[已隐藏]", line)
    line = _URL_CREDENTIALS.sub("[已隐藏]@", line)
    if len(lines) > 1 or len(line) > _LINE_LIMIT:
        return line[:_LINE_LIMIT - 1].rstrip() + "…"
    return line


def _computer_detail(args: dict) -> str:
    action = args.get("action")
    label = _COMPUTER_ACTIONS.get(action, "操作界面")
    if action == "navigate" and isinstance(args.get("url"), str):
        return f"{label} {re.split(r'[?#]', args['url'], maxsplit=1)[0]}"
    if action in {"click", "double_click", "right_click", "download"} and isinstance(args.get("selector"), str):
        return f"{label} {args['selector']}"
    if action in {"click", "double_click", "right_click", "move", "drag"} and {"x", "y"} <= args.keys():
        point = f"({args['x']}, {args['y']})"
        if action == "drag" and {"end_x", "end_y"} <= args.keys():
            point += f" → ({args['end_x']}, {args['end_y']})"
        return f"{label} {point}"
    if action in {"key", "press"} and isinstance(args.get("key"), str):
        return f"{label} {args['key']}"
    if action == "scroll":
        direction = {"up": "↑", "down": "↓", "left": "←", "right": "→"}.get(args.get("direction"))
        if direction and "amount" in args:
            return f"{label} {direction} {args['amount']}"
    return label


def _call_detail(call: dict) -> str:
    args = call["arguments"]
    if call["tool"] == "computer_use" and args.get("action") == "type":
        return "输入文本"
    command = call.get("command") or args.get("command")
    if isinstance(command, str) and (preview := _one_line(command)):
        return preview
    if call["tool"] == "computer_use":
        return _one_line(_computer_detail(args))
    return "执行工具"


def _call_status(result: dict | None, status: str) -> str:
    if result is None:
        return "unknown" if status in _FINISHED else "running"
    if result.get("outcome_unknown"):
        return "unknown"
    return "failed" if result.get("error") or result.get("exit_code", 0) else "success"


def progress_card(events: Iterable[dict], status: str) -> dict | None:
    """Render the newest tool calls and counts for the whole run."""
    calls, results = {}, {}
    for event in events:
        data = event["data"]
        if event["type"] == "tool_call":
            calls[data["call_id"]] = data
        elif event["type"] == "tool_result":
            results[data["call_id"]] = data["result"]
    if not calls:
        return None

    states = {call_id: _call_status(results.get(call_id), status) for call_id in calls}
    counts = Counter(states.values())
    summary = ["**执行工具**", _RUN_STATUS.get(status, "进行中"), f"{len(calls)} 条"]
    summary.extend(f"{label} {counts[state]}" for state, (_, label) in _CALL_STATUS.items() if counts[state])
    if len(calls) > _VISIBLE_CALLS:
        summary.append(f"最近 {_VISIBLE_CALLS} 条")
    rows = [
        f"{_CALL_STATUS[states[call_id]][0]}  {_call_detail(calls[call_id])}"
        for call_id in list(calls)[-_VISIBLE_CALLS:]
    ]
    return {
        "schema": "2.0",
        "config": {"update_multi": True},
        "body": {
            "padding": "6px 10px 6px 10px",
            "vertical_spacing": "2px",
            "elements": [
                {"tag": "markdown", "text_size": "notation", "content": " · ".join(summary)},
                {"tag": "div", "text": {"tag": "plain_text", "text_size": "notation", "content": "\n".join(rows)}},
            ],
        },
    }
