import unittest
from unittest.mock import AsyncMock, patch

from harness.core.agent import Agent
from harness.core.llm import ModelError, TransientModelError


class Store:
    def __init__(self):
        self.items = []

    def event(self, run_id, kind, data):
        self.items.append({"type": kind, "data": data})

    def events(self, run_id, **kwargs):
        return self.items


class ModelRetryTest(unittest.IsolatedAsyncioTestCase):
    async def test_interrupted_generation_is_retried_without_combining_deltas(self):
        store = Store()
        class Model:
            calls = 0
            async def stream_complete(self, messages, tools, *, on_delta):
                self.calls += 1
                if self.calls == 1:
                    on_delta({"content": "partial"})
                    raise TransientModelError("interrupted")
                on_delta({"content": "complete"})
                return {"role": "assistant", "content": "complete"}
        model = Model()
        agent = Agent(None, store, None, model)
        with patch("harness.core.agent.asyncio.sleep", new_callable=AsyncMock):
            await agent._complete_turn("run", [])
        self.assertEqual([e["type"] for e in store.items],
                         ["assistant_delta", "model_retry", "assistant_delta", "assistant"])
        first, second = [e["data"] for e in store.items if e["type"] == "assistant_delta"]
        self.assertNotEqual(first["generation_id"], second["generation_id"])
        self.assertEqual(store.items[-1]["data"]["message"]["content"], "complete")

    async def test_retries_are_bounded_and_nontransient_errors_are_not_retried(self):
        for error, expected in [(TransientModelError("interrupted"), 3), (ModelError("invalid"), 1)]:
            model = type("Model", (), {})()
            model.stream_complete = AsyncMock(side_effect=error)
            agent = Agent(None, Store(), None, model)
            with patch("harness.core.agent.asyncio.sleep", new_callable=AsyncMock):
                with self.assertRaises(type(error)):
                    await agent._complete_turn("run", [])
            self.assertEqual(model.stream_complete.await_count, expected)
