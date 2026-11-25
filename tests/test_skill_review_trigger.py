import tempfile
import unittest
from pathlib import Path

from harness.storage.sqlite import RunStore


class SkillReviewTriggerTest(unittest.TestCase):
    def test_accumulates_iterations_across_runs_and_resets_after_queueing(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RunStore(Path(directory))
            first = store.create("First task", None, None)
            self.assertFalse(self.finish(store, first, 5))
            second = store.create("Continue", first["session_id"], None)
            self.assertTrue(self.finish(store, second, 5))
            third = store.create("Continue again", first["session_id"], None)
            self.assertFalse(self.finish(store, third, 1))

    def test_short_correction_triggers_review_in_existing_session(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RunStore(Path(directory))
            first = store.create("Make a chart", None, None)
            self.assertFalse(self.finish(store, first, 1))
            second = store.create("不对，改成柱状图", first["session_id"], None)
            self.assertTrue(self.finish(store, second, 0))

    @staticmethod
    def finish(store, run, iterations):
        store.transition(run["id"], {"queued"}, "running")
        for _ in range(iterations):
            store.event(run["id"], "assistant", {"message": {"content": ""}, "calls": [{"call_id": "x"}]})
        store.transition(run["id"], {"running"}, "completed", skill_review="auto")
        return any(event["type"] == "skill_review" for event in store.events(run["id"]))
