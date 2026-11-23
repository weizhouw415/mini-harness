import tempfile
import unittest
from pathlib import Path

import httpx

from harness.api.server import create_app
from harness.config import Settings
from tests.test_skills import document


class SkillApiTest(unittest.IsolatedAsyncioTestCase):
    async def test_completed_run_can_publish_and_roll_back_evaluated_versions(self):
        versions = iter(["1.0.0", "1.1.0"])

        class LearningModel:
            async def complete(self, messages, tools):
                if tools:
                    raise AssertionError("Learning should not expose execution tools")
                return {"content": document(next(versions))}

        async def verify_fixture(skill, environment):
            return {
                "passed": environment == "cloud-browser" and "verify the uploaded file" in skill["content"],
                "fixture": "csv-roundtrip",
            }

        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(
                data_dir=Path(directory),
                api_token="skills-api-test-token",
                worker_secret="skills-worker-test-secret",
            )
            app = create_app(settings, llm=LearningModel(), skill_evaluator=verify_fixture)
            run = app.state.store.create("Process and upload a CSV", None, None)
            app.state.store.transition(run["id"], {"queued"}, "completed", output="CSV uploaded")

            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver",
                headers={"Authorization": f"Bearer {settings.api_token}"},
            ) as client:
                for version in ("1.0.0", "1.1.0"):
                    skill = f"/v1/skills/csv-roundtrip/{version}"
                    learned = (await client.post(f"/v1/runs/{run['id']}/learn")).raise_for_status().json()
                    self.assertEqual(learned["state"], "candidate")
                    self.assertEqual(learned["version"], version)

                    too_early = await client.post(f"{skill}/publish", json={"approved": True})
                    self.assertEqual(too_early.status_code, 422)

                    evaluated = (
                        await client.post(f"{skill}/evaluate", json={"environment": "cloud-browser"})
                    ).raise_for_status().json()
                    self.assertEqual(evaluated["environment_status"], "passed")

                    declined = await client.post(f"{skill}/publish", json={"approved": False})
                    self.assertEqual(declined.status_code, 422)
                    published = (
                        await client.post(f"{skill}/publish", json={"approved": True})
                    ).raise_for_status().json()
                    self.assertEqual(published["state"], "active")

                restored = (
                    await client.post("/v1/skills/csv-roundtrip/1.0.0/rollback", json={"approved": True})
                ).raise_for_status().json()
                self.assertEqual(restored["version"], "1.0.0")
                index = (
                    await client.get("/v1/skills", params={"environment": "macos-desktop"})
                ).raise_for_status().json()
                self.assertEqual(
                    {entry["version"]: entry["state"] for entry in index}, {"1.0.0": "active", "1.1.0": "deprecated"}
                )
                self.assertTrue(all(entry["environment_status"] == "untested" for entry in index))
