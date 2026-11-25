import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock

from harness.config import MemoryConfig, Settings
from harness.core.agent import Agent
from harness.memory import MemoryStore
from harness.memory.jobs import MemoryJobs
from harness.storage.sqlite import RunStore


def test_memory_toggle_controls_agent_injection_and_job_enqueue():
    async def exercise():
        with TemporaryDirectory() as directory:
            settings = Settings(data_dir=Path(directory))
            runs = RunStore(settings.data_dir)
            memories = MemoryStore(runs)
            config = MemoryConfig(settings)
            MemoryJobs(runs, memories, None, config)
            first = runs.create("网页设计", None, None, memory_input="网页设计")
            memories.remember(first["session_id"], "style", "喜欢简洁的网页设计")
            seen = []
            agent = Agent(settings, runs, None, None,
                          memory_store=memories, memory_config=config)
            agent._prepare_uploads = AsyncMock()
            agent._collect_skill_candidates = AsyncMock(return_value=None)

            async def complete(run_id, messages, enabled):
                seen.append(messages)
                runs.event(run_id, "assistant", {"message": {"role": "assistant", "content": "完成"}, "calls": []})

            agent._complete_turn = complete
            runs.transition(first["id"], {"queued"}, "running")
            await agent._loop(runs.get(first["id"]), {})
            assert "喜欢简洁的网页设计" not in str(seen[-1])
            with runs.connect() as db:
                assert db.execute("SELECT COUNT(*) FROM memory_jobs").fetchone()[0] == 0

            config.save({"enabled": True, "automatic": True, "personal": False})
            next_run = runs.create("网页设计", first["session_id"], None, memory_input="网页设计")
            runs.transition(next_run["id"], {"queued"}, "running")
            await agent._loop(runs.get(next_run["id"]), {})
            assert "喜欢简洁的网页设计" in str(seen[-1])
            with runs.connect() as db:
                assert db.execute("SELECT COUNT(*) FROM memory_jobs WHERE run_id=?", (next_run["id"],)).fetchone()[0] == 1

    asyncio.run(exercise())


def test_extraction_job_survives_restart_and_does_not_restore_forgotten_memory():
    async def exercise():
        with TemporaryDirectory() as directory:
            settings = Settings(data_dir=Path(directory))
            runs = RunStore(settings.data_dir)
            memories = MemoryStore(runs)
            config = MemoryConfig(settings)
            config.save({"enabled": True, "automatic": True, "personal": False})

            class Extractor:
                async def complete(self, messages, tools):
                    return {"content": '{"memories":[{"key":"design.style","content":"喜欢简洁设计","kind":"preference"}]}'}

            jobs = MemoryJobs(runs, memories, Extractor(), config)
            run = runs.create("我喜欢简洁设计", None, None, memory_input="我喜欢简洁设计")
            runs.transition(run["id"], {"queued"}, "completed", output="收到", memory_extract=True)
            claimed = jobs._claim(True)
            assert claimed and claimed["run_id"] == run["id"]
            await jobs._process(claimed)
            found = memories.search(run["session_id"], "简洁")
            assert len(found) == 1
            memories.forget(run["session_id"], found[0]["id"])
            with runs.connect() as db:
                db.execute("UPDATE memory_jobs SET status='queued', lease_token=NULL, lease_until=NULL")
            restarted = MemoryJobs(runs, memories, Extractor(), config)
            retry = restarted._claim(True)
            await restarted._process(retry)
            assert memories.search(run["session_id"], "简洁") == []

    asyncio.run(exercise())
