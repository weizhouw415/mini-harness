import json
import tempfile
import unittest
from pathlib import Path

from harness.core.context import build_messages
from harness.storage.sqlite import RunStore


class SessionHistoryTest(unittest.TestCase):
    def test_reasoning_is_excluded_from_previous_and_current_requests(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RunStore(Path(directory))
            first = store.create("first", None, None)
            original = {"message": {"role": "assistant", "content": "answer",
                                    "reasoning_content": "private previous reasoning"}, "calls": []}
            store.event(first["id"], "assistant", original)
            store.transition(first["id"], {"queued"}, "completed", output="answer")
            current = store.create("next", first["session_id"], None)
            previous = store.previous(first["session_id"], current["id"])
            call = {"call_id": "c1", "provider_id": "p1", "name": "bash", "arguments": "{}"}
            events = [
                {"type": "assistant", "data": {"message": {"role": "assistant", "content": None,
                 "reasoning_content": "private current reasoning", "tool_calls": []}, "calls": [call]}},
                {"type": "tool_result", "data": {"call_id": "c1", "result": {"stdout": "ok"}}},
            ]
            messages = build_messages(current, {}, [], previous, events, Path(directory), 1000)
            serialized = json.dumps(messages)
            self.assertNotIn("reasoning_content", serialized)
            self.assertNotIn("private previous reasoning", serialized)
            self.assertNotIn("private current reasoning", serialized)
            self.assertEqual(previous[0]["events"][0]["data"]["message"]["reasoning_content"],
                             "private previous reasoning")
            self.assertEqual(events[0]["data"]["message"]["reasoning_content"], "private current reasoning")

    def test_failed_cancelled_and_completed_runs_include_full_events(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RunStore(Path(directory))
            first = store.create("原始问题", None, None)
            store.transition(first["id"], {"queued"}, "completed", output="原始答复")
            second = store.create("最新进展", first["session_id"], None)
            detail = "重要资料" * 5000
            store.event(second["id"], "tool_result", {"call_id": "call1", "result": {"stdout": detail}})
            store.transition(second["id"], {"queued"}, "failed", error="Maximum number of model steps reached.")
            third = store.create("继续调查", first["session_id"], None)
            store.event(third["id"], "tool_call", {"call_id": "call2", "tool": "bash", "arguments": {}})
            store.transition(third["id"], {"queued"}, "cancelled")
            current = store.create("continue", first["session_id"], None)
            previous = store.previous(first["session_id"], current["id"])
            messages = build_messages(current, {}, [], previous, [], Path(directory), 1000)
            self.assertEqual([run["status"] for run in previous], ["completed", "failed", "cancelled"])
            self.assertIn(detail, messages[5]["content"])
            record = json.loads(messages[5]["content"].split("\n", 1)[1])
            self.assertEqual(record["error"], "Maximum number of model steps reached.")
            self.assertEqual(record["events"][0]["data"]["result"]["stdout"], detail)
            self.assertIn("tool_call", messages[7]["content"])
            self.assertEqual(messages[-1]["content"], "continue")
            self.assertEqual(messages[4]["content"], "最新进展")

    def test_current_results_are_not_discarded_at_small_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            call = {"call_id": "c1", "provider_id": "provider1", "name": "bash", "arguments": "{}"}
            text = "result" * 10000
            events = [
                {"type": "assistant", "data": {"message": {
                    "role": "assistant", "content": None, "tool_calls": [{
                        "id": "provider1", "type": "function", "function": {"name": "bash", "arguments": "{}"},
                    }],
                }, "calls": [call]}},
                {"type": "tool_result", "data": {"call_id": "c1", "result": {"stdout": text}}},
            ]
            messages = build_messages({"task": "test"}, {}, [], [], events, Path(directory), 1000)
            self.assertEqual(json.loads(messages[-1]["content"])["stdout"], text)
