from dataclasses import dataclass

from harness.core.approval_shell import is_safe_bash, is_safe_bash_legacy
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
        # Old Workers retain the previous rule. New Workers isolate PATH and
        # their shell environment, allowing additional read-only commands.
        if is_safe_bash_legacy(args["command"], allow_ls=cloud):
            return ApprovalDecision(False, "匹配简单终端命令规则，不读取指定文件或执行 Shell 展开。")
        if capabilities.get("safe_bash_path") is True and is_safe_bash(args["command"]):
            return ApprovalDecision(False, "匹配只读终端命令规则。")
        if cloud and trust_cloud_bash:
            return ApprovalDecision(False, "服务端配置了信任此类浏览器 Worker 的终端命令。")
        return ApprovalDecision(True, "命令未匹配自动放行的只读规则，需要人工确认。")

    action = args["action"]
    target = args.get("target", "browser" if cloud else "desktop")
    targets = capabilities.get("computer_targets")
    actions = targets.get(target) if isinstance(targets, dict) else capabilities.get("actions")
    if capabilities.get("computer") is not True or not isinstance(actions, list) or action not in actions:
        return ApprovalDecision(True, "此 Worker 未声明支持该操作的自动审批。")
    fields = set(args) - {"target"}
    if action in {"observe", "screenshot"} and fields == {"action"}:
        if target == "browser":
            return ApprovalDecision(False, "仅截图或观察当前浏览器页面。")
        if desktop:
            return ApprovalDecision(False, "仅查看当前已授权桌面 Worker 的屏幕。")
    if target == "browser" and action == "scroll" and fields <= {"action", "dx", "dy"}:
        if all(abs(args.get(axis, 0)) <= 4_000 for axis in ("dx", "dy")):
            return ApprovalDecision(False, "仅滚动当前浏览器页面，未移动指针。")
    if target == "browser":
        return ApprovalDecision(True, "操作超出浏览器观察和滚动规则，需要人工确认。")
    return ApprovalDecision(True, "此操作可能使用你的实际桌面、鼠标或键盘，需要人工确认。")
