import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import httpx

from harness.api.server import create_app
from harness.config import Settings
from tests.harness_support import eventually, tool_call, wait_for_run
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
                app.state.skills.create_candidate(document("2.0.0"))
                old_draft = "/v1/skills/csv-roundtrip/2.0.0"
                too_early = await client.post(f"{old_draft}/publish", json={"approved": True})
                self.assertEqual(too_early.status_code, 422)
                evaluated = (await client.post(
                    f"{old_draft}/evaluate", json={"environment": "cloud-browser"},
                )).raise_for_status().json()
                self.assertEqual(evaluated["environment_status"], "passed")
                declined = await client.post(f"{old_draft}/publish", json={"approved": False})
                self.assertEqual(declined.status_code, 422)

                for version in ("1.0.0", "1.1.0"):
                    learned = (await client.post(f"/v1/runs/{run['id']}/learn")).raise_for_status().json()
                    self.assertEqual(learned["state"], "active")
                    self.assertEqual(learned["version"], version)
                    self.assertEqual(learned["evaluations"], {})

                restored = (
                    await client.post("/v1/skills/csv-roundtrip/1.0.0/rollback", json={"approved": True})
                ).raise_for_status().json()
                self.assertEqual(restored["version"], "1.0.0")
                index = (
                    await client.get("/v1/skills", params={"environment": "macos-desktop"})
                ).raise_for_status().json()
                self.assertEqual(
                    {entry["version"]: entry["state"] for entry in index},
                    {"1.0.0": "active", "1.1.0": "deprecated", "2.0.0": "candidate"},
                )
                self.assertTrue(all(entry["environment_status"] == "untested" for entry in index))

    async def test_background_review_saves_skill_and_next_run_can_patch_it(self):
        def manage(payload):
            return "harness skill manage <<'JSON'\n" + json.dumps(payload) + "\nJSON"

        def bash(identifier, command):
            return tool_call(identifier, "bash", {"command": command})

        class Worker:
            commands = []

            async def acquire(self, worker_id, run_id, workspace_id, *, on_reserved):
                worker = SimpleNamespace(worker_id="worker", capabilities={"environment": "cloud-browser", "bash": True})
                on_reserved(worker)
                return worker

            async def bash(self, worker_id, run_id, call_id, command, timeout):
                self.commands.append(command)
                return {"exit_code": 0, "stdout": "done", "stderr": ""}

            async def release(self, worker_id, run_id):
                pass

        class Model:
            def __init__(self, assertions):
                self.assertions = assertions

            async def complete(self, messages, tools):
                self.assertions.assertEqual([tool["function"]["name"] for tool in tools], ["bash"])
                results = [json.loads(message["content"]) for message in messages if message["role"] == "tool"]
                step = len(results)
                if messages[0]["content"].startswith("Review a completed run"):
                    if step == 0:
                        return bash("review-shell", "printf should-not-run")
                    if step == 1:
                        self.assertions.assertEqual(results[-1]["exit_code"], 1)
                        return bash("create", manage({"action": "create", "name": "csv-roundtrip", "content": document()}))
                    self.assertions.assertEqual(results[-1]["exit_code"], 0)
                    return {"content": "Skill saved."}
                if messages[1]["content"] == "Process ten CSV inputs":
                    if step < 10:
                        return bash(f"work-{step}", f"printf {step}")
                    return {"content": "CSV work completed."}
                if step == 0:
                    return bash("view", "harness skill view csv-roundtrip")
                if step == 1:
                    record = json.loads(results[-1]["stdout"])
                    self.assertions.assertIn("verify the uploaded file", record["document"])
                    return bash("patch", manage({
                        "action": "patch", "name": record["name"], "base_version": record["version"],
                        "old_string": "verify the uploaded file", "new_string": "verify the uploaded row count",
                    }))
                self.assertions.assertTrue(results[-1]["skill_changed"])
                return {"content": "Skill improved."}

        with tempfile.TemporaryDirectory() as directory:
            worker = Worker()
            settings = Settings(data_dir=Path(directory), api_token="token")
            app = create_app(settings, llm=Model(self), worker_manager=worker)
            async with app.router.lifespan_context(app), httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver",
                headers={"Authorization": "Bearer token"},
            ) as client:
                first = (await client.post("/v1/runs", json={
                    "task": "Process ten CSV inputs", "approval_mode": "allow",
                })).raise_for_status().json()
                self.assertEqual((await wait_for_run(client, first["id"]))["status"], "completed")
                review = await eventually(lambda: next((
                    event["data"] for event in app.state.store.events(first["id"]) if event["type"] == "skill_review"
                ), None))
                self.assertEqual(review, {"status": "completed", "changes": [
                    {"action": "create", "name": "csv-roundtrip", "version": "1.0.0"},
                ]})

                second = (await client.post("/v1/runs", json={"task": "Improve the CSV Skill"})).raise_for_status().json()
                self.assertEqual((await wait_for_run(client, second["id"]))["status"], "completed")
                self.assertEqual(worker.commands, [f"printf {step}" for step in range(10)])
                current = app.state.skills.get("csv-roundtrip")
                self.assertEqual((current["state"], current["version"], current["evaluations"]), ("active", "1.0.1", {}))
                self.assertIn("verify the uploaded row count", current["content"])
                self.assertEqual(app.state.skills.active_index("windows-desktop"), [])
