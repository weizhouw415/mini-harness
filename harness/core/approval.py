from dataclasses import dataclass

from harness.core.approval_shell import is_safe_bash
from harness.storage.sqlite import ApprovalMode


@dataclass(frozen=True)
class ApprovalDecision:
    needs_operator: bool
    reason: str | None = None


def decide_approval(mode: ApprovalMode, tool: str, args: dict, capabilities: dict, *,
                    trust_cloud_bash: bool) -> ApprovalDecision:
    environment = capabilities.get("environment")
    cloud = environment == "cloud-browser"
    desktop = environment in {"macos-desktop", "windows-desktop"}
    if mode == "allow":
        return ApprovalDecision(False)
    if mode == "manual":
        return ApprovalDecision(True)
    if mode == "legacy":
        return ApprovalDecision(not cloud or (tool == "bash" and not trust_cloud_bash))

    if tool == "bash":
        if not (cloud or desktop) or capabilities.get("bash") is not True:
            return ApprovalDecision(True, "此 Worker 的终端命令需要人工确认。")
        # Desktop Bash inherits the user's PATH. Only allow Bash builtins there.
        if is_safe_bash(args["command"], allow_ls=cloud):
            return ApprovalDecision(False, "匹配简单终端命令规则，不读取指定文件或执行 Shell 展开。")
        if cloud and trust_cloud_bash:
            return ApprovalDecision(False, "服务端配置了信任此类浏览器 Worker 的终端命令。")
        return ApprovalDecision(True, "命令超出简单终端命令规则，需要人工确认。")

    action = args["action"]
    actions = capabilities.get("actions")
    if capabilities.get("computer") is not True or not isinstance(actions, list) or action not in actions:
        return ApprovalDecision(True, "此 Worker 未声明支持该操作的自动审批。")
    if action in {"observe", "screenshot"} and set(args) == {"action"}:
        if cloud:
            return ApprovalDecision(False, "仅截图或观察当前浏览器页面。")
        if desktop:
            return ApprovalDecision(False, "仅查看当前已授权桌面 Worker 的屏幕。")
    if cloud and action == "scroll" and set(args) <= {"action", "dx", "dy"}:
        if all(abs(args.get(axis, 0)) <= 4_000 for axis in ("dx", "dy")):
            return ApprovalDecision(False, "仅滚动当前浏览器页面，未移动指针。")
    if cloud:
        return ApprovalDecision(True, "操作超出浏览器观察和滚动规则，需要人工确认。")
    return ApprovalDecision(True, "此桌面操作需要人工确认。")
