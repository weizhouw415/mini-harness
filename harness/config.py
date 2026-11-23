from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    data_dir: Path = Path(".harness")
    api_token: str = ""
    worker_secret: str | dict[str, str] = ""
    model: str = ""
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: str = ""
    trust_cloud_bash: bool = False
    max_steps: int = 500
    context_chars: int = 48_000
    max_tool_seconds: int = 120

    @classmethod
    def from_env(cls) -> Settings:
        worker_secrets = os.environ.get("HARNESS_WORKER_SECRETS")
        worker_secret = (
            json.loads(worker_secrets) if worker_secrets else os.environ.get("HARNESS_WORKER_SECRET", "")
        )

        if not isinstance(worker_secret, (str, dict)) or (
            isinstance(worker_secret, dict)
            and not all(isinstance(value, str) and value for value in worker_secret.values())
        ):
            raise ValueError("HARNESS_WORKER_SECRETS must be a JSON object of worker IDs to secrets")
        return cls(
            data_dir=Path(os.environ.get("HARNESS_DATA_DIR", ".harness")),
            api_token=os.environ.get("HARNESS_API_TOKEN", ""),
            worker_secret=worker_secret,
            model=os.environ.get("HARNESS_MODEL", ""),
            llm_base_url=os.environ.get("HARNESS_LLM_BASE_URL", "https://api.openai.com/v1"),
            llm_api_key=os.environ.get("HARNESS_LLM_API_KEY", ""),
            trust_cloud_bash=os.environ.get("HARNESS_TRUST_CLOUD_BASH") == "1",
            max_steps=int(os.environ.get("HARNESS_MAX_STEPS", "500")),
            context_chars=int(os.environ.get("HARNESS_CONTEXT_CHARS", "48000")),
            max_tool_seconds=int(os.environ.get("HARNESS_MAX_TOOL_SECONDS", "120")),
        )


class ModelConfig:
    def __init__(self, settings: Settings):
        self.path = settings.data_dir / "model.json"
        try:
            self.values = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            self.values = {
                "model": settings.model,
                "base_url": settings.llm_base_url.rstrip("/"),
                "api_key": settings.llm_api_key,
            }

    def save(self, model: str, base_url: str, api_key: str | None):
        if api_key is None:
            api_key = self.values["api_key"] if base_url == self.values["base_url"] else ""
        values = {"model": model, "base_url": base_url, "api_key": api_key}
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent,
                                         prefix=".model-", delete=False) as file:
            path = Path(file.name)
            try:
                json.dump(values, file, ensure_ascii=False)
                file.close()
                path.replace(self.path)
            finally:
                path.unlink(missing_ok=True)
        self.values = values
