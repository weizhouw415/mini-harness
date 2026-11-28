import json
import unittest

from harness.channels.feishu_progress import progress_card


def call(call_id, command, *, tool="bash", arguments=None):
    return {
        "type": "tool_call",
        "data": {"call_id": call_id, "tool": tool, "command": command, "arguments": arguments or {}},
    }


def result(call_id, **value):
    return {"type": "tool_result", "data": {"call_id": call_id, "result": value}}


def content(events, status):
    summary, commands = progress_card(events, status)["body"]["elements"]
    return summary["content"], commands["text"]["content"]


class FeishuProgressTest(unittest.TestCase):
    def test_card_summarizes_whole_run_and_lists_only_recent_calls(self):
        events = [call(index, f"echo command-{index}") for index in range(7)] + [
            result(0, exit_code=0), result(1, exit_code=0), result(2, exit_code=0),
            result(3, exit_code=2), result(4, error="connection lost", outcome_unknown=True),
            result(5, exit_code=0), result(99, error="never dispatched"),
        ]
        card = progress_card(events, "running")
        self.assertTrue(card["config"]["update_multi"])
        summary, commands = content(events, "running")
        for label in ("7 条", "成功 4", "未成功 1", "结果未知 1", "执行中 1", "最近 5 条"):
            self.assertIn(label, summary)
        self.assertEqual(commands.splitlines(), [
            "✓  echo command-2",
            "×  echo command-3",
            "?  echo command-4",
            "✓  echo command-5",
            "…  echo command-6",
        ])
        summary, commands = content(events + [result(6, exit_code=0)], "completed")
        self.assertIn("已完成", summary)
        self.assertIn("成功 5", summary)
        self.assertTrue(commands.endswith("✓  echo command-6"))

    def test_preview_keeps_literal_shell_text_and_omits_sensitive_values(self):
        literal = "echo '<at id=all></at>' `pwd`"
        events = [
            call(1, None, arguments={"command": literal}),
            call(2, "API_TOKEN='secret value' curl -H 'Authorization: Bearer token-value' https://example.com"),
            call(3, "harness computer type 'private form text'", tool="computer_use",
                 arguments={"action": "type", "text": "private form text"}),
            call(4, "printf " + "x" * 200 + "\nsecond line"),
            call(5, "harness computer click 120 34", tool="computer_use",
                 arguments={"action": "click", "x": 120, "y": 34}),
        ]
        card = progress_card(events, "running")
        self.assertEqual(card["body"]["elements"][1]["text"]["tag"], "plain_text")
        data = json.dumps(card, ensure_ascii=False)
        for value in ("secret value", "token-value", "private form text", "second line"):
            self.assertNotIn(value, data)
        _, commands = content(events, "running")
        self.assertIn(literal, commands)
        self.assertIn("API_TOKEN=[已隐藏]", commands)
        self.assertIn("Authorization: Bearer [已隐藏]", commands)
        self.assertIn("输入文本", commands)
        self.assertTrue(commands.splitlines()[3].endswith("…"))
        self.assertIn("harness computer click 120 34", commands)

    def test_absent_tool_calls_have_no_card(self):
        self.assertIsNone(progress_card([result(1, error="never dispatched")], "failed"))
