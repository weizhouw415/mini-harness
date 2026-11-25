import json
import tempfile
import unittest
from pathlib import Path

from harness.memory.jobs import MemoryJobs
from harness.memory.store import MemoryStore
from harness.storage.sqlite import RunStore


class Config:
    values = {"enabled": True, "automatic": True, "personal": False}


class FakeModel:
    def __init__(self, responses):
        self.responses = iter(responses)

    async def complete(self, messages, tools):
        return {"content": json.dumps(next(self.responses), ensure_ascii=False)}


class MemoryJobsTest(unittest.IsolatedAsyncioTestCase):
    async def test_extraction_episode_and_expiration(self):
        with tempfile.TemporaryDirectory() as directory:
            runs = RunStore(Path(directory))
            memories = MemoryStore(runs)
            model = FakeModel([
                {"memories": [{"key": "travel", "content": "在东京逗留", "kind": "short_term",
                               "expires_in_days": 3}]},
                {"summary": "用户请求处理东京项目，任务已完成。"},
            ])
            jobs = MemoryJobs(runs, memories, model, Config())
            run = runs.create("我这三天在东京，请处理项目", None, None)
            with runs.connect() as db:
                jobs.save_input(db, run["id"], run["task"])
            for index in range(4):
                runs.event(run["id"], "tool_call", {"call_id": str(index)})
            runs.transition(run["id"], {"queued"}, "completed", output="已完成")
            with runs.connect() as db:
                jobs.enqueue_extract(db, run["id"])
            await jobs._process(jobs._claim(True))
            found = memories.search(run["session_id"], "", limit=10)
            self.assertEqual({item["kind"] for item in found}, {"short_term", "episode"})
            with runs.connect() as db:
                db.execute("UPDATE memory_expirations SET expires_at='2000-01-01T00:00:00+00:00'")
            jobs._enqueue_dream()
            await jobs._process(jobs._claim(True))
            self.assertEqual([item["kind"] for item in memories.search(run["session_id"], "", limit=10)], ["episode"])

    async def test_semantic_merge_skips_concurrently_updated_record(self):
        with tempfile.TemporaryDirectory() as directory:
            runs = RunStore(Path(directory))
            memories = MemoryStore(runs)
            run = runs.create("test", None, None)
            first = memories.remember(run["session_id"], "one", "喜欢简洁", source_run_id=run["id"])
            second = memories.remember(run["session_id"], "two", "偏爱简洁", source_run_id=run["id"])

            class ConcurrentModel:
                async def complete(self, messages, tools):
                    memories.remember(run["session_id"], "two", "偏爱复杂")
                    return {"content": json.dumps({"merges": [{"keep_id": first["id"], "remove_id": second["id"]}]})}

            jobs = MemoryJobs(runs, memories, ConcurrentModel(), Config())
            jobs._enqueue_dream()
            await jobs._process(jobs._claim(True))
            self.assertEqual(len(memories.search(run["session_id"], "", limit=10)), 2)
            self.assertEqual(memories.get(run["session_id"], second["id"])["content"], "偏爱复杂")

    async def test_semantic_merge_keeps_evidence_and_forget_cascades(self):
        with tempfile.TemporaryDirectory() as directory:
            runs = RunStore(Path(directory))
            memories = MemoryStore(runs)
            run = runs.create("test", None, None)
            first = memories.remember(run["session_id"], "one", "喜欢简洁", source_run_id=run["id"])
            second = memories.remember(run["session_id"], "two", "偏爱简洁", source_run_id=run["id"])
            model = FakeModel([{"merges": [{"keep_id": first["id"], "remove_id": second["id"]}]}])
            jobs = MemoryJobs(runs, memories, model, Config())
            jobs._enqueue_dream()
            await jobs._process(jobs._claim(True))
            self.assertEqual(len(memories.search(run["session_id"], "", limit=10)), 1)
            with runs.connect() as db:
                self.assertEqual(db.execute("SELECT count(*) FROM memory_merged_versions").fetchone()[0], 1)
            memories.forget(run["session_id"], first["id"])
            with runs.connect() as db:
                self.assertEqual(db.execute("SELECT count(*) FROM memory_merged_versions").fetchone()[0], 0)
                self.assertEqual(db.execute("SELECT count(*) FROM memory_tombstones").fetchone()[0], 2)
