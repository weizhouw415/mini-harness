from __future__ import annotations

from typing import Protocol

import httpx

from harness.config import ModelConfig


class LLM(Protocol):
    async def complete(self, messages: list[dict], tools: list[dict]) -> dict: ...


class ModelError(RuntimeError):
    pass


class OpenAICompatible:
    def __init__(self, config: ModelConfig):
        self.config = config

    async def complete(self, messages: list[dict], tools: list[dict]) -> dict:
        config = self.config.values
        if not config["model"]:
            raise ModelError("Set HARNESS_MODEL to configure the model.")
        payload = {"model": config["model"], "messages": messages}
        if tools:
            payload["tools"] = tools
        headers = {}
        if config["api_key"]:
            headers["Authorization"] = "Bearer " + config["api_key"]
        url = config["base_url"].rstrip("/") + "/chat/completions"
        try:
            async with httpx.AsyncClient(timeout=90, follow_redirects=False) as client:
                response = await client.post(url, headers=headers, json=payload)
                response.raise_for_status()
            body = response.json()
            message = body["choices"][0]["message"]
            if not isinstance(message, dict):
                raise TypeError("Invalid assistant message")
            return {**message, "_usage": body.get("usage")}
        except httpx.HTTPStatusError as error:
            raise ModelError(f"Model service returned HTTP {error.response.status_code}.") from error
        except httpx.RequestError as error:
            raise ModelError(f"Model request failed ({type(error).__name__}).") from error
        except (KeyError, IndexError, TypeError, ValueError) as error:
            raise ModelError("Model response did not contain a valid assistant message.") from error
