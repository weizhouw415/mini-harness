import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from harness.api.server import create_app
from harness.config import ModelConfig, Settings
from harness.core.llm import OpenAICompatible


HEADERS = {"Sec-Fetch-Site": "same-origin", "X-Harness-UI": "1"}


class WebProvidersTest(unittest.IsolatedAsyncioTestCase):
    async def test_provider_credentials_switching_and_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(data_dir=Path(directory), api_token="test-token", worker_secret="worker-secret")
            app = create_app(settings, local_ui=True)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://127.0.0.1") as client:
                first = {"name": "First", "model": "model-a", "base_url": "https://example.test/v1", "api_key": "secret-a"}
                second = {"name": "Second", "model": "model-b", "base_url": "https://example.test/v1", "api_key": "secret-b"}
                for provider in (first, second):
                    (await client.post("/ui/providers", headers=HEADERS, json=provider)).raise_for_status()
                listed = (await client.get("/ui/providers", headers=HEADERS)).raise_for_status().json()
                self.assertNotIn("secret-a", json.dumps(listed))
                self.assertNotIn("secret-b", json.dumps(listed))
                providers = {item["name"]: item for item in listed["providers"]}
                self.assertEqual(set(providers), {"First", "Second"})
                self.assertTrue(all(item["has_api_key"] for item in providers.values()))
                first_id, second_id = providers["First"]["id"], providers["Second"]["id"]

                (await client.put(f"/ui/providers/{first_id}/activate", headers=HEADERS)).raise_for_status()
                active = (await client.get("/ui/model", headers=HEADERS)).raise_for_status().json()
                self.assertEqual(active["model"], "model-a")
                # Updating one provider without a key preserves its own key, even when URLs match.
                (await client.put(f"/ui/providers/{first_id}", headers=HEADERS, json={
                    "name": "First", "model": "model-a2", "base_url": first["base_url"],
                })).raise_for_status()
                (await client.put(f"/ui/providers/{second_id}/activate", headers=HEADERS)).raise_for_status()
                self.assertEqual((await client.get("/ui/model", headers=HEADERS)).json()["model"], "model-b")
                self.assertEqual((settings.data_dir / "model.json").stat().st_mode & 0o777, 0o600)
                self.assertEqual(ModelConfig(settings).values["api_key"], "secret-b")
                (await client.put(f"/ui/providers/{first_id}/activate", headers=HEADERS)).raise_for_status()
                self.assertEqual(ModelConfig(settings).values["api_key"], "secret-a")
                (await client.put(f"/ui/providers/{second_id}/activate", headers=HEADERS)).raise_for_status()
                self.assertEqual((await client.get("/ui/providers")).status_code, 403)
                self.assertEqual((await client.post("/ui/providers", json=first)).status_code, 403)
                self.assertEqual((await client.get("/ui/providers", headers={**HEADERS, "Host": "example.test"})).status_code, 403)
                self.assertEqual((await client.post("/ui/providers", headers=HEADERS, json={
                    **first, "base_url": "https://user:password@example.test/v1",
                })).status_code, 422)

            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(settings, local_ui=True)),
                                         base_url="http://127.0.0.1") as client:
                restored = (await client.get("/ui/providers", headers=HEADERS)).raise_for_status().json()
                self.assertEqual(restored["active_provider_id"], second_id)
                self.assertEqual({p["id"] for p in restored["providers"]}, {first_id, second_id})
                self.assertEqual((await client.get("/ui/model", headers=HEADERS)).json()["model"], "model-b")
                cleared = (await client.put(f"/ui/providers/{second_id}", headers=HEADERS, json={
                    **second, "api_key": "",
                })).raise_for_status().json()
                self.assertFalse(next(p for p in cleared["providers"] if p["id"] == second_id)["has_api_key"])
                (await client.put(f"/ui/providers/{second_id}", headers=HEADERS, json=second)).raise_for_status()
                # Changing a provider's URL without supplying a key must not send the old key to a new host.
                (await client.put(f"/ui/providers/{first_id}", headers=HEADERS, json={
                    "name": "First", "model": "model-a2", "base_url": "https://other.test/v1",
                })).raise_for_status()
                updated = (await client.get("/ui/providers", headers=HEADERS)).json()
                self.assertFalse(next(p for p in updated["providers"] if p["id"] == first_id)["has_api_key"])
                self.assertTrue(next(p for p in updated["providers"] if p["id"] == second_id)["has_api_key"])
                self.assertEqual((await client.delete(f"/ui/providers/{second_id}")).status_code, 403)
                remaining = (await client.delete(f"/ui/providers/{second_id}", headers=HEADERS)).raise_for_status().json()
                self.assertEqual(remaining["active_provider_id"], first_id)
                self.assertEqual([p["id"] for p in remaining["providers"]], [first_id])
                empty = (await client.delete(f"/ui/providers/{first_id}", headers=HEADERS)).raise_for_status().json()
                self.assertEqual(empty["providers"], [])
                self.assertIsNone(empty["active_provider_id"])
                self.assertFalse(empty["configured"])
                self.assertEqual((await client.put(f"/ui/providers/{first_id}/activate", headers=HEADERS)).status_code, 404)

    async def test_selected_provider_drives_model_request(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(data_dir=Path(directory), api_token="test-token", worker_secret="worker-secret")
            app = create_app(settings, local_ui=True)
            requests = []

            def handle(request):
                requests.append(request)
                return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]})

            client_class = httpx.AsyncClient
            async with client_class(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client:
                for name in ("a", "b"):
                    (await client.post("/ui/providers", headers=HEADERS, json={
                        "name": name, "model": f"model-{name}",
                        "base_url": f"https://{name}.example.test/v1", "api_key": f"key-{name}",
                    })).raise_for_status()
                providers = (await client.get("/ui/providers", headers=HEADERS)).json()["providers"]
                for provider in providers:
                    (await client.put(f"/ui/providers/{provider['id']}/activate", headers=HEADERS)).raise_for_status()
                    model = OpenAICompatible(ModelConfig(settings))
                    with patch("harness.core.llm.httpx.AsyncClient", side_effect=lambda **kwargs: client_class(
                        **kwargs, transport=httpx.MockTransport(handle),
                    )):
                        await model.complete([{"role": "user", "content": "hello"}], [])
                    request = requests[-1]
                    self.assertEqual(str(request.url), f"https://{provider['name']}.example.test/v1/chat/completions")
                    self.assertEqual(json.loads(request.content)["model"], f"model-{provider['name']}")
                    self.assertEqual(request.headers["Authorization"], f"Bearer key-{provider['name']}")

    async def test_environment_defaults_and_empty_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(data_dir=Path(directory), api_token="test-token", worker_secret="worker-secret", model="env-model", llm_base_url="https://env.test/v1",
                                llm_api_key="env-secret")
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(settings, local_ui=True)),
                                         base_url="http://127.0.0.1") as client:
                status = (await client.get("/ui/providers", headers=HEADERS)).raise_for_status().json()
                self.assertTrue(status["configured"])
                self.assertEqual(status["providers"][0]["model"], "env-model")
                self.assertTrue(status["providers"][0]["has_api_key"])
                self.assertNotIn("env-secret", json.dumps(status))

        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(data_dir=Path(directory), api_token="test-token", worker_secret="worker-secret")
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(settings, local_ui=True)),
                                         base_url="http://127.0.0.1") as client:
                status = (await client.get("/ui/providers", headers=HEADERS)).raise_for_status().json()
                self.assertFalse(status["configured"])
                self.assertEqual(status["providers"], [])
                self.assertIsNone(status["active_provider_id"])

    async def test_legacy_configuration_is_available_after_migration(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "model.json"
            path.write_text(json.dumps({"model": "legacy-model", "base_url": "https://legacy.test/v1",
                                        "api_key": "legacy-secret"}), encoding="utf-8")
            settings = Settings(data_dir=Path(directory), api_token="test-token", worker_secret="worker-secret")
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(settings, local_ui=True)),
                                         base_url="http://127.0.0.1") as client:
                status = (await client.get("/ui/providers", headers=HEADERS)).raise_for_status().json()
                self.assertTrue(status["configured"])
                self.assertEqual(len(status["providers"]), 1)
                provider = status["providers"][0]
                self.assertEqual(provider["model"], "legacy-model")
                self.assertEqual(provider["base_url"], "https://legacy.test/v1")
                self.assertTrue(provider["has_api_key"])
                self.assertNotIn("legacy-secret", json.dumps(status))
                self.assertEqual(status["active_provider_id"], provider["id"])
