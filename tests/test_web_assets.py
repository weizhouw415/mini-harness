import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

import harness.api.server as server
from harness.config import Settings


class WebAssetVersionTest(unittest.IsolatedAsyncioTestCase):
    async def test_running_app_keeps_ui_assets_paired_with_loaded_routes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copytree(Path(server.__file__).with_name("web"), root / "web")
            settings = Settings(data_dir=root / "data", api_token="token", worker_secret="secret")
            with patch.object(server, "__file__", str(root / "server.py")):
                app = server.create_app(settings, local_ui=True)
                previous_index = (root / "web" / "index.html").read_bytes()
                previous_script = (root / "web" / "app.js").read_bytes()
                (root / "web" / "index.html").write_text("updated index", encoding="utf-8")
                (root / "web" / "app.js").write_text("updated script", encoding="utf-8")

                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                             base_url="http://127.0.0.1") as client:
                    index = (await client.get("/")).raise_for_status()
                    script = (await client.get("/ui/app.js")).raise_for_status()
                    self.assertEqual(index.content, previous_index)
                    self.assertEqual(script.content, previous_script)
                    self.assertTrue(index.headers["content-type"].startswith("text/html"))
                    self.assertTrue(script.headers["content-type"].startswith("text/javascript"))
                    self.assertEqual(index.headers["cache-control"], "no-cache")
                    self.assertEqual(script.headers["cache-control"], "no-cache")
                    self.assertIn("default-src 'self'", index.headers["content-security-policy"])
                    self.assertEqual(index.headers["x-content-type-options"], "nosniff")
                    self.assertEqual((await client.get("/ui/nonexistent")).status_code, 404)
                    self.assertEqual((await client.get("/ui/index.html")).status_code, 404)

                updated_app = server.create_app(settings, local_ui=True)
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=updated_app),
                                             base_url="http://127.0.0.1") as client:
                    self.assertEqual((await client.get("/")).text, "updated index")
                    self.assertEqual((await client.get("/ui/app.js")).text, "updated script")
