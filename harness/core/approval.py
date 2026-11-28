from dataclasses import dataclass

from harness.core.approval_shell import destructive_command_reason
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
        reason = destructive_command_reason(args["command"])
        if reason:
            return ApprovalDecision(True, reason)
        return ApprovalDecision(False, "自动审批已放行此终端命令。")

    action = args["action"]
    target = args.get("target", "browser" if cloud else "desktop")
    targets = capabilities.get("computer_targets")
    actions = targets.get(target) if isinstance(targets, dict) else capabilities.get("actions")
    required_action = "screenshot" if action == "screen_info" else action
    if capabilities.get("computer") is not True or not isinstance(actions, list) or required_action not in actions:
        return ApprovalDecision(True, "此 Worker 未声明支持该操作的自动审批。")
    return ApprovalDecision(False, "自动审批已放行此界面操作。")
