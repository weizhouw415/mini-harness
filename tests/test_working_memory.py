import json
import tempfile
import unittest
from pathlib import Path

from harness.core.context import build_messages
from harness.memory.working import working_checkpoint
from harness.storage.sqlite import RunStore


class LLM:
    def __init__(self, fail=False):
        self.fail = fail
        self.inputs = []

    async def complete(self, messages, tools):
        self.inputs.append(json.loads(messages[1]["content"]))
        if self.fail:
            raise RuntimeError("model failed")
        return {"content": json.dumps({"goal": "finish", "constraints": [], "completed": ["first"],
                                       "pending": ["second"], "paths": ["file.txt"], "current_issue": ""})}


class WorkingMemoryTest(unittest.IsolatedAsyncioTestCase):
    async def test_large_traces_are_summarized_in_bounded_batches(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RunStore(Path(directory))
            run = store.create("task", None, None)
            for number in range(8):
                call = {"call_id": str(number), "provider_id": str(number), "name": "bash", "arguments": "{}"}
                store.event(run["id"], "assistant", {"message": {"role": "assistant", "content": ""}, "calls": [call]})
                output = "a" * 200_000 + " /workspace/result.txt " + "b" * 200_000 if number == 0 else "x" * 45_000
                store.event(run["id"], "tool_result", {"call_id": str(number), "result": {"stdout": output}})
            events = store.events(run["id"])
            llm = LLM()
            checkpoint = await working_checkpoint(run, events, 100, llm, store)
            self.assertGreater(len(llm.inputs), 1)
            self.assertTrue(all(len(json.dumps(item, ensure_ascii=False)) < 250_000 for item in llm.inputs))
            self.assertIn("/workspace/result.txt", str(llm.inputs[0]))
            assistants = [event for event in events if event["type"] == "assistant"]
            self.assertEqual(checkpoint["through_seq"], assistants[-2]["seq"])
            self.assertIsNotNone(llm.inputs[1]["previous_checkpoint"])

    async def test_checkpoint_preserves_latest_group_and_recovers_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RunStore(Path(directory))
            run = store.create("original task", None, None)
            for number in (1, 2):
                call = {"call_id": str(number), "provider_id": str(number), "name": "bash", "arguments": "{}"}
                message = {"role": "assistant", "content": None, "tool_calls": [{"id": str(number)}]}
                store.event(run["id"], "assistant", {"message": message, "calls": [call]})
                store.event(run["id"], "tool_result", {"call_id": str(number), "result": {"stdout": str(number) * 1000}})
            events = store.events(run["id"])
            checkpoint = await working_checkpoint(run, events, 100, LLM(), store)
            self.assertIsNotNone(checkpoint)
            events = store.events(run["id"])
            restored = await working_checkpoint(run, events, 100, LLM(fail=True), store)
            self.assertEqual(restored, checkpoint)
            messages = build_messages(run, {}, [], [], events, Path(directory), 100,
                                      working_checkpoint=restored)
            self.assertIn("original task", str(messages))
            self.assertIn("file.txt", str(messages))
            self.assertNotIn("1" * 1000, str(messages))
            self.assertIn("2" * 1000, str(messages))

    async def test_model_failure_preserves_history(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RunStore(Path(directory))
            run = store.create("task", None, None)
            for number in (1, 2):
                call = {"call_id": str(number), "provider_id": str(number), "name": "bash", "arguments": "{}"}
                store.event(run["id"], "assistant", {"message": {"role": "assistant", "content": ""}, "calls": [call]})
                store.event(run["id"], "tool_result", {"call_id": str(number), "result": {"stdout": "x" * 1000}})
            events = store.events(run["id"])
            self.assertIsNone(await working_checkpoint(run, events, 100, LLM(fail=True), store))
            messages = build_messages(run, {}, [], [], events, Path(directory), 100)
            self.assertEqual(str(messages).count("x" * 1000), 2)
