import tempfile
import unittest
from pathlib import Path

import httpx

from harness.api.server import create_app
from harness.config import Settings


class WebModelTest(unittest.IsolatedAsyncioTestCase):
    async def test_local_access_and_model_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(data_dir=Path(directory), api_token="test-token", worker_secret="worker-secret")
            app = create_app(settings, local_ui=True)
            local_headers = {"Sec-Fetch-Site": "same-origin", "X-Harness-UI": "1"}
            config = {"model": "test-model", "base_url": "https://example.test/v1", "api_key": "test-key"}
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1",
            ) as client:
                self.assertEqual((await client.get("/v1/runs")).status_code, 401)
                self.assertEqual((await client.get("/v1/runs", headers=local_headers)).status_code, 200)
                self.assertEqual((await client.get("/v1/runs", headers={
                    **local_headers, "Sec-Fetch-Site": "cross-site",
                })).status_code, 401)
                self.assertEqual((await client.put("/ui/model", json=config)).status_code, 403)
                self.assertEqual((await client.put("/ui/model", json=config, headers={
                    **local_headers, "Host": "example.test",
                })).status_code, 403)
                missing = (await client.get("/ui/model", headers=local_headers)).raise_for_status().json()
                self.assertFalse(missing["configured"])
                saved = (await client.put("/ui/model", headers=local_headers, json=config)).raise_for_status().json()
                self.assertTrue(saved["has_api_key"])
                self.assertNotIn("api_key", saved)
                self.assertEqual((settings.data_dir / "model.json").stat().st_mode & 0o777, 0o600)
                reused = (await client.put("/ui/model", headers=local_headers, json={
                    "model": "renamed", "base_url": config["base_url"],
                })).raise_for_status().json()
                self.assertTrue(reused["has_api_key"])
                changed = (await client.put("/ui/model", headers=local_headers, json={
                    "model": "renamed", "base_url": "http://localhost:1234/v1",
                })).raise_for_status().json()
                self.assertFalse(changed["has_api_key"])

            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(settings, local_ui=True)),
                                         base_url="http://127.0.0.1") as client:
                persisted = (await client.get("/ui/model", headers=local_headers)).raise_for_status().json()
                self.assertEqual(persisted, changed)

            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("192.0.2.1", 12345)),
                                         base_url="http://127.0.0.1") as client:
                self.assertEqual((await client.get("/v1/runs", headers=local_headers)).status_code, 401)
