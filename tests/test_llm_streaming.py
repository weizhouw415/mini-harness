import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from harness.core.llm import ModelError, OpenAICompatible


def _event(delta=None, *, finish=None, usage=None):
    choices = [] if delta is None else [{"index": 0, "delta": delta, "finish_reason": finish}]
    return ("data: " + json.dumps({"choices": choices, "usage": usage}, ensure_ascii=False) + "\r\n\r\n").encode()


class _Frames(httpx.AsyncByteStream):
    def __init__(self, frames):
        self.frames = frames

    async def __aiter__(self):
        for frame in self.frames:
            if callable(frame):
                frame()
            else:
                yield frame


class ModelStreamingTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.model = OpenAICompatible(SimpleNamespace(values={
            "model": "fixture", "api_key": "test", "base_url": "https://model.test/v1",
        }))
        self.requests = []

    def _mock(self, response):
        def handle(request):
            self.requests.append(request)
            return response

        client = httpx.AsyncClient
        return patch("harness.core.llm.httpx.AsyncClient", side_effect=lambda **kwargs: client(
            **kwargs, transport=httpx.MockTransport(handle),
        ))

    async def test_emits_progress_before_more_network_data_and_assembles_interleaved_calls(self):
        updates = []
        reasoning = _event({"reasoning_content": "正在检查"})
        boundary = reasoning.index("正".encode()) + 1

        def calls(index, **kwargs):
            return {"tool_calls": [{"index": index, **kwargs}]}

        frames = _Frames([
            b"\xef\xbb\xbf: heartbeat\r\n\r\n", reasoning[:boundary], reasoning[boundary:],
            lambda: self.assertEqual(updates, [{"reasoning_content": "正在检查"}]),
            _event({"reasoning": "。", "content": "One "}),
            _event(calls(1, id="call_b", type="function", function={"name": "bash", "arguments": '{"command":'})),
            _event(calls(0, id="call_a", type="function", function={"name": "bash", "arguments": '{"command":'})),
            _event(calls(1, function={"arguments": '"ls"}'})),
            _event(calls(0, function={"arguments": '"pwd"}'})),
            _event({"content": "moment."}), _event({}, finish="tool_calls"),
            b'data: {"choices": [],\r\ndata: "usage": {"total_tokens": 42}}\r\n\r\n',
            b"data: [DONE]\r\n\r\n",
        ])
        response = httpx.Response(200, headers={"content-type": "text/event-stream; charset=utf-8"}, stream=frames)
        with self._mock(response):
            result = await self.model.stream_complete([{"role": "user", "content": "task"}], [], on_delta=updates.append)

        self.assertEqual(result["content"], "One moment.")
        self.assertEqual(result["reasoning_content"], "正在检查。")
        self.assertEqual(result["_usage"], {"total_tokens": 42})
        self.assertEqual([item["id"] for item in result["tool_calls"]], ["call_a", "call_b"])
        self.assertEqual([json.loads(item["function"]["arguments"]) for item in result["tool_calls"]], [
            {"command": "pwd"}, {"command": "ls"},
        ])
        self.assertEqual(updates[-2:], [{"reasoning_content": "。", "content": "One "}, {"content": "moment."}])
        [request] = self.requests
        self.assertIs(json.loads(request.content)["stream"], True)
        self.assertEqual(json.loads(request.content)["stream_options"], {"include_usage": True})
        self.assertEqual(request.extensions["timeout"]["read"], 900)

    async def test_wrapper_streams_once_when_provider_responds_with_ordinary_json(self):
        body = {"choices": [{"message": {"role": "assistant", "content": "answer", "reasoning": "plan"}}],
                "usage": {"total_tokens": 3}}
        with self._mock(httpx.Response(200, json=body)):
            result = await self.model.complete([], [])
        self.assertEqual(result["content"], "answer")
        self.assertEqual(result["reasoning_content"], "plan")
        self.assertEqual(result["_usage"], {"total_tokens": 3})
        [request] = self.requests
        self.assertIs(json.loads(request.content)["stream"], True)

    async def test_incomplete_or_limited_calls_never_become_a_completion(self):
        partial = _event({"tool_calls": [{"index": 0, "id": "call_a", "type": "function",
                                         "function": {"name": "bash", "arguments": '{"command":'}}]})
        for finish in (b"", b"data: [DONE]\n\n", _event({}, finish="length"), _event({}, finish="tool_calls")):
            with self.subTest(finish=finish):
                response = httpx.Response(200, headers={"content-type": "text/event-stream"}, content=partial + finish)
                with self._mock(response), self.assertRaises(ModelError):
                    await self.model.complete([], [])

    async def test_limit_reports_last_partial_text_and_reasoning_before_error(self):
        updates = []
        delta = {"content": "unfinished", "reasoning_content": "partial plan"}
        response = httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  content=_event(delta, finish="length"))
        with self._mock(response), self.assertRaisesRegex(ModelError, "output limit"):
            await self.model.stream_complete([], [], on_delta=updates.append)
        self.assertEqual(updates, [delta])

    async def test_full_finish_at_eof_and_provider_errors(self):
        headers = {"content-type": "text/event-stream"}
        with self._mock(httpx.Response(200, headers=headers, content=_event({"content": "done"}, finish="stop"))):
            self.assertEqual((await self.model.complete([], []))["content"], "done")
        provider_error = b'event: error\ndata: {"error": {"message": "fixture failed"}}\n\n'
        with self._mock(httpx.Response(200, headers=headers, content=provider_error)):
            with self.assertRaisesRegex(ModelError, "fixture failed"):
                await self.model.complete([], [])
        with self._mock(httpx.Response(400, json={"error": {"message": "unsupported model"}})):
            with self.assertRaisesRegex(ModelError, r"HTTP 400\. unsupported model"):
                await self.model.complete([], [])
