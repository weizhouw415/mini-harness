from __future__ import annotations

import json
import os
import tempfile
import uuid
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

    @property
    def skills_dir(self) -> Path:
        return Path(os.environ.get("HARNESS_SKILLS_DIR") or self.data_dir / "skills").expanduser().resolve()

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
        self.default_base_url = settings.llm_base_url.rstrip("/")
        try:
            stored = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            stored = {
                "model": settings.model,
                "base_url": self.default_base_url,
                "api_key": settings.llm_api_key,
            }
        self.unconfigured_values = {"model": "", "base_url": self.default_base_url, "api_key": ""}
        if "providers" in stored:
            self.providers = stored["providers"]
            self.active_provider_id = stored.get("active_provider_id")
        else:
            # Legacy configuration and environment defaults become the first provider.
            self.providers = ([{"id": "default", "name": "默认供应商", **stored}]
                              if (stored.get("model") or stored.get("api_key")
                                  or stored.get("base_url") != "https://api.openai.com/v1") else [])
            self.active_provider_id = "default" if self.providers else None
            if not self.providers:
                self.unconfigured_values = stored

    @property
    def values(self):
        provider = next((p for p in self.providers if p["id"] == self.active_provider_id), None)
        return provider or self.unconfigured_values

    def _write(self, providers, active_provider_id):
        values = {"providers": providers, "active_provider_id": active_provider_id}
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent,
                                         prefix=".model-", delete=False) as file:
            path = Path(file.name)
            try:
                json.dump(values, file, ensure_ascii=False)
                file.close()
                path.replace(self.path)
            finally:
                path.unlink(missing_ok=True)
        self.providers, self.active_provider_id = providers, active_provider_id
        self.unconfigured_values = {"model": "", "base_url": self.default_base_url, "api_key": ""}

    def provider(self, provider_id):
        return next((p for p in self.providers if p["id"] == provider_id), None)

    def create(self, name, model, base_url, api_key):
        provider = {"id": uuid.uuid4().hex, "name": name, "model": model,
                    "base_url": base_url, "api_key": api_key or ""}
        self._write([*self.providers, provider], self.active_provider_id or provider["id"])

    def update(self, provider_id, name, model, base_url, api_key):
        previous = self.provider(provider_id)
        if previous is None:
            raise KeyError(provider_id)
        if api_key is None:
            api_key = previous["api_key"] if base_url == previous["base_url"] else ""
        provider = {"id": provider_id, "name": name, "model": model,
                    "base_url": base_url, "api_key": api_key}
        self._write([provider if p["id"] == provider_id else p for p in self.providers],
                    self.active_provider_id)

    def activate(self, provider_id):
        if self.provider(provider_id) is None:
            raise KeyError(provider_id)
        self._write(self.providers, provider_id)

    def delete(self, provider_id):
        if self.provider(provider_id) is None:
            raise KeyError(provider_id)
        providers = [p for p in self.providers if p["id"] != provider_id]
        active = self.active_provider_id
        if active == provider_id:
            active = providers[0]["id"] if providers else None
        self._write(providers, active)

    def save(self, model: str, base_url: str, api_key: str | None):
        if self.active_provider_id:
            self.update(self.active_provider_id, self.values["name"], model, base_url, api_key)
        else:
            if api_key is None:
                api_key = self.values["api_key"] if base_url == self.values["base_url"] else ""
            self.create("默认供应商", model, base_url, api_key)


class CapabilityConfig:
    """Persist the optional commands exposed to new runs."""

    DEFAULTS = {"view": True, "register": True, "computer_use": True}

    def __init__(self, settings: Settings):
        self.path = settings.data_dir / "capabilities.json"
        try:
            stored = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            stored = {}
        if not isinstance(stored, dict) or any(
            key in stored and type(stored[key]) is not bool for key in self.DEFAULTS
        ):
            raise ValueError("Invalid capability configuration")
        self._values = {key: stored.get(key, default) for key, default in self.DEFAULTS.items()}

    @property
    def values(self) -> dict[str, bool]:
        return self._values.copy()

    def save(self, values: dict[str, bool]) -> None:
        if set(values) != set(self.DEFAULTS) or any(type(value) is not bool for value in values.values()):
            raise ValueError("Invalid capability configuration")
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent,
                                         prefix=".capabilities-", delete=False) as file:
            path = Path(file.name)
            try:
                json.dump(values, file, ensure_ascii=False)
                file.close()
                path.replace(self.path)
            finally:
                path.unlink(missing_ok=True)
        self._values = values.copy()


class MemoryConfig:
    """Persist the memory feature and its scope for this installation."""

    DEFAULTS = {"enabled": False, "automatic": True, "personal": False}

    def __init__(self, settings: Settings):
        self.path = settings.data_dir / "memory-settings.json"
        try:
            stored = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            stored = {}
        if not isinstance(stored, dict) or any(
            key in stored and type(stored[key]) is not bool for key in self.DEFAULTS
        ):
            raise ValueError("Invalid memory configuration")
        self._values = {key: stored.get(key, default) for key, default in self.DEFAULTS.items()}

    @property
    def values(self) -> dict[str, bool]:
        return self._values.copy()

    def save(self, values: dict[str, bool]) -> None:
        if set(values) != set(self.DEFAULTS) or any(type(value) is not bool for value in values.values()):
            raise ValueError("Invalid memory configuration")
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent,
                                         prefix=".memory-settings-", delete=False) as file:
            path = Path(file.name)
            try:
                json.dump(values, file, ensure_ascii=False)
                file.close()
                path.replace(self.path)
            finally:
                path.unlink(missing_ok=True)
        self._values = values.copy()
