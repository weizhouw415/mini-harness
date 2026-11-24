from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from io import StringIO
from typing import Protocol

import httpx

from harness.config import ModelConfig


class LLM(Protocol):
    async def complete(self, messages: list[dict], tools: list[dict]) -> dict: ...


class ModelError(RuntimeError):
    pass


DeltaHandler = Callable[[dict[str, str]], None]
_REASONING_FIELDS = ("reasoning_content", "reasoning", "reasoning_text")
_TOOL_FINISH = {"tool_calls", "function_call"}
_SUCCESSFUL_FINISH = {"stop", "end_turn", *_TOOL_FINISH}


def _reasoning(delta: dict) -> str | None:
    for name in _REASONING_FIELDS:
        value = delta.get(name)
        if isinstance(value, str) and value:
            return value
    return None


def _error_detail(error: object) -> str:
    message = error.get("message") if isinstance(error, dict) else error
    return f" {message[:500]}" if isinstance(message, str) and message else ""


def _check_finish(reason: object) -> None:
    if reason is None or isinstance(reason, str) and reason in _SUCCESSFUL_FINISH:
        return
    if reason == "length":
        raise ModelError("Model reached its output limit before completing the response.")
    if reason == "content_filter":
        raise ModelError("Model service filtered the response.")
    raise ModelError("Model service did not finish the response successfully.")


def _primary_choice(choices: object) -> dict | None:
    if not isinstance(choices, list) or any(not isinstance(choice, dict) for choice in choices):
        raise TypeError("Invalid model choices")
    return next((choice for choice in choices if choice.get("index", 0) == 0), None)


@dataclass
class _ToolCall:
    identifier: str = ""
    name: str = ""
    arguments: StringIO = field(default_factory=StringIO)

    def append(self, chunk: dict) -> None:
        kind = chunk.get("type")
        function = chunk.get("function")
        if kind not in (None, "function") or function is not None and not isinstance(function, dict):
            raise ModelError("Model stream contained a malformed tool call.")
        function = function or {}
        identifier, name, arguments = chunk.get("id"), function.get("name"), function.get("arguments")
        if any(value is not None and not isinstance(value, str) for value in (identifier, name, arguments)):
            raise ModelError("Model stream contained a malformed tool call.")
        if identifier and identifier != self.identifier:
            self.identifier += identifier
        if name and name != self.name:
            self.name += name
        if arguments:
            self.arguments.write(arguments)
        if len(self.identifier) > 512 or len(self.name) > 512 or self.arguments.tell() > 65_536:
            raise ModelError("Model streamed an oversized tool call.")

    def result(self) -> dict:
        arguments = self.arguments.getvalue()
        if not self.identifier or not self.name:
            raise ModelError("Model stream contained an incomplete tool call.")
        try:
            parsed = json.loads(arguments)
        except ValueError as error:
            raise ModelError("Model stream contained incomplete or invalid tool arguments.") from error
        if not isinstance(parsed, dict):
            raise ModelError("Model tool arguments must be a JSON object.")
        return {"id": self.identifier, "type": "function", "function": {"name": self.name, "arguments": arguments}}


class _StreamedReply:
    def __init__(self, on_delta: DeltaHandler | None):
        self.on_delta = on_delta
        self.content: list[str] = []
        self.reasoning: list[str] = []
        self.calls: dict[int, _ToolCall] = {}
        self.finish: str | None = None
        self.usage = None
        self.saw_choice = False

    def add(self, body: dict) -> None:
        if body.get("usage") is not None:
            self.usage = body["usage"]
        choice = _primary_choice(body.get("choices", []))
        if choice is None:
            return
        self.saw_choice = True
        delta = choice.get("delta")
        if delta is None:
            delta = {}
        if not isinstance(delta, dict) or self.finish is not None and any(value for value in delta.values()):
            raise ModelError("Model stream contained an invalid assistant delta.")
        content = delta.get("content")
        if content is not None and not isinstance(content, str):
            raise ModelError("Model stream contained invalid assistant content.")
        reasoning = _reasoning(delta)
        updates = {}
        if reasoning:
            self.reasoning.append(reasoning)
            updates["reasoning_content"] = reasoning
        if content:
            self.content.append(content)
            updates["content"] = content
        calls = delta.get("tool_calls")
        if calls is not None:
            if not isinstance(calls, list) or len(calls) > 8:
                raise ModelError("Model streamed an invalid number of tool calls.")
            for call in calls:
                index = call.get("index") if isinstance(call, dict) else None
                if type(index) is not int or not 0 <= index < 8:
                    raise ModelError("Model stream contained an invalid tool-call index.")
                if index not in self.calls:
                    self.calls[index] = _ToolCall()
                self.calls[index].append(call)
        if self.on_delta is not None and updates:
            self.on_delta(updates)
        finish = choice.get("finish_reason")
        _check_finish(finish)
        if finish is not None:
            if self.finish is not None and self.finish != finish:
                raise ModelError("Model stream contained conflicting finish reasons.")
            self.finish = finish

    def result(self, done: bool) -> dict:
        if not self.saw_choice or not done and self.finish is None:
            raise ModelError("Model stream ended before the response completed.")
        if self.calls and self.finish is None or self.finish in _TOOL_FINISH and not self.calls:
            raise ModelError("Model stream ended before the tool calls completed.")
        message = {"role": "assistant", "content": "".join(self.content) or None, "_usage": self.usage}
        if self.reasoning:
            message["reasoning_content"] = "".join(self.reasoning)
        if self.calls:
            if set(self.calls) != set(range(len(self.calls))):
                raise ModelError("Model stream omitted part of the tool-call list.")
            calls = [self.calls[index].result() for index in range(len(self.calls))]
            if len({call["id"] for call in calls}) != len(calls):
                raise ModelError("Model stream repeated a tool-call ID.")
            message["tool_calls"] = calls
        return message


async def _sse_events(response: httpx.Response) -> AsyncIterator[tuple[str, str]]:
    response.encoding = "utf-8"
    first = True
    event = ""
    data: list[str] = []
    async for line in response.aiter_lines():
        if first:
            line = line.removeprefix("\ufeff")
            first = False
        if not line:
            if data:
                yield event, "\n".join(data)
            event, data = "", []
            continue
        name, _, value = line.partition(":")
        value = value.removeprefix(" ")
        if name == "data":
            data.append(value)
        elif name == "event":
            event = value
    if data:
        raise ModelError("Model stream ended inside an event.")


async def _sse_reply(response: httpx.Response, on_delta: DeltaHandler | None) -> dict:
    streamed = _StreamedReply(on_delta)
    async for event, data in _sse_events(response):
        if event == "error":
            try:
                body = json.loads(data)
            except ValueError:
                body = None
            detail = _error_detail(body.get("error", body)) if isinstance(body, dict) else ""
            raise ModelError(f"Model service reported a stream error.{detail}")
        if data.strip() == "[DONE]":
            return streamed.result(done=True)
        if not data.strip():
            continue
        body = json.loads(data)
        if not isinstance(body, dict):
            raise TypeError("Invalid model stream chunk")
        if body.get("error") is not None:
            raise ModelError(f"Model service reported a stream error.{_error_detail(body['error'])}")
        streamed.add(body)
    return streamed.result(done=False)


async def _json_reply(response: httpx.Response, on_delta: DeltaHandler | None) -> dict:
    await response.aread()
    body = response.json()
    if not isinstance(body, dict):
        raise TypeError("Invalid model response")
    if body.get("error") is not None:
        raise ModelError(f"Model service returned an error.{_error_detail(body['error'])}")
    choice = _primary_choice(body.get("choices"))
    if choice is None or not isinstance(message := choice.get("message"), dict):
        raise TypeError("Invalid assistant message")
    result = {**message, "_usage": body.get("usage")}
    progress = {}
    if reasoning := _reasoning(message):
        result["reasoning_content"] = reasoning
        progress["reasoning_content"] = reasoning
    content = message.get("content")
    if isinstance(content, str) and content:
        progress["content"] = content
    if on_delta is not None and progress:
        on_delta(progress)
    _check_finish(choice.get("finish_reason"))
    return result


class OpenAICompatible:
    def __init__(self, config: ModelConfig):
        self.config = config

    async def complete(self, messages: list[dict], tools: list[dict]) -> dict:
        return await self.stream_complete(messages, tools)

    async def stream_complete(self, messages: list[dict], tools: list[dict], *,
                              on_delta: DeltaHandler | None = None) -> dict:
        config = self.config.values
        if not config["model"]:
            raise ModelError("Set HARNESS_MODEL to configure the model.")
        payload = {"model": config["model"], "messages": messages, "stream": True,
                   "stream_options": {"include_usage": True}}
        if tools:
            payload["tools"] = tools
        headers = {"Accept": "text/event-stream, application/json"}
        if config["api_key"]:
            headers["Authorization"] = "Bearer " + config["api_key"]
        url = config["base_url"].rstrip("/") + "/chat/completions"
        try:
            timeout = httpx.Timeout(90, read=15 * 60)
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
                async with client.stream("POST", url, headers=headers, json=payload) as response:
                    if not response.is_success:
                        detail = ""
                        try:
                            error_payload = bytearray()
                            async for chunk in response.aiter_bytes():
                                if len(error_payload) + len(chunk) > 64 * 1024:
                                    break
                                error_payload.extend(chunk)
                            body = json.loads(error_payload)
                            if isinstance(body, dict):
                                detail = _error_detail(body.get("error"))
                        except (ValueError, httpx.RequestError):
                            pass
                        raise ModelError(f"Model service returned HTTP {response.status_code}.{detail}")
                    content_type = response.headers.get("content-type", "").partition(";")[0].strip().lower()
                    if content_type == "text/event-stream":
                        return await _sse_reply(response, on_delta)
                    return await _json_reply(response, on_delta)
        except httpx.RequestError as error:
            raise ModelError(f"Model request failed ({type(error).__name__}).") from error
        except (KeyError, IndexError, TypeError, ValueError) as error:
            raise ModelError("Model response did not contain a valid assistant message.") from error
