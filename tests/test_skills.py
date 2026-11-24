import sqlite3
import tempfile
import unittest
from pathlib import Path

from harness.skills import SkillError, SkillStore, evaluate_skill, learn_skill


def document(version="1.0.0"):
    return f"""---
name: csv-roundtrip
description: 下载 CSV 后处理并上传
version: {version}
environments:
  - cloud-browser
  - macos-desktop
---

Process the rows and verify the uploaded file.
"""


class SkillLifecycleTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name)
        self.store = SkillStore(self.path)

    async def test_learning_evaluation_publication_and_rollback(self):
        async def generate(messages):
            self.assertIn("fixture passed", messages[1]["content"])
            self.assertNotIn("fixture-password", messages[1]["content"])
            return document()

        trace = {
            "run": {"status": "completed"},
            "events": [
                {"arguments": {"action": "type", "text": "fixture-password"}},
                {"arguments": '{"action": "type", "text": "fixture-password"}'},
                {"message": "fixture passed", "password": "fixture-password"},
            ],
        }
        learned = await learn_skill(self.store, trace, generate)
        self.assertEqual(learned["state"], "active")
        self.assertEqual(
            (self.path / "versions/csv-roundtrip/1.0.0/SKILL.md").read_text(encoding="utf-8"),
            document(),
        )
        self.assertEqual([entry["version"] for entry in self.store.match("处理 CSV", "cloud-browser")], ["1.0.0"])
        self.store.create_candidate(document("1.1.0"))

        async def verifier(skill, environment):
            self.assertEqual(skill["state"], "testing")
            self.assertEqual(environment, "cloud-browser")
            self.assertIn("verify the uploaded file", skill["content"])
            return {"passed": True, "checks": {"upload_matches": True}}

        evaluated = await evaluate_skill(self.store, "csv-roundtrip", "1.1.0", "cloud-browser", verifier)
        self.assertEqual(evaluated["environment_status"], "passed")
        self.assertEqual(evaluated["state"], "candidate")
        with self.assertRaisesRegex(SkillError, "explicit approval"):
            self.store.publish("csv-roundtrip", "1.1.0", approved=False)

        self.store.publish("csv-roundtrip", "1.1.0", approved=True)
        match = self.store.match("处理文件并上传", "cloud-browser")
        self.assertEqual([entry["version"] for entry in match], ["1.1.0"])
        self.assertIn("Process the rows", match[0]["content"])
        self.assertEqual(self.store.match("处理 CSV", "macos-desktop"), [])
        self.assertEqual(self.store.index("macos-desktop")[0]["environment_status"], "untested")

        self.assertEqual(self.store.get("csv-roundtrip", "1.0.0")["state"], "deprecated")
        self.assertEqual(self.store.get("csv-roundtrip")["version"], "1.1.0")
        self.store.rollback("csv-roundtrip", "1.0.0", approved=True)
        reopened = SkillStore(self.path)
        self.assertEqual(reopened.get("csv-roundtrip")["version"], "1.0.0")
        self.assertEqual(reopened.get("csv-roundtrip", "1.1.0")["state"], "deprecated")

    async def test_failed_evaluation_can_retry_without_granting_use(self):
        self.store.create_candidate(document())

        async def bad_verifier(skill, environment):
            return {"passed": "yes"}

        failed = await evaluate_skill(self.store, "csv-roundtrip", "1.0.0", "cloud-browser", bad_verifier)
        self.assertEqual(failed["environment_status"], "failed")
        with self.assertRaises(SkillError):
            self.store.publish("csv-roundtrip", "1.0.0", approved=True)

        async def good_verifier(skill, environment):
            return {"passed": True}

        retried = await evaluate_skill(self.store, "csv-roundtrip", "1.0.0", "cloud-browser", good_verifier)
        self.assertEqual(retried["environment_status"], "passed")

    async def test_learning_can_skip_and_requires_a_completed_run(self):
        async def generate(messages):
            return "NO_SKILL"

        with self.assertRaisesRegex(SkillError, "completed run"):
            await learn_skill(self.store, {"run": {"status": "paused"}}, generate)
        skipped = await learn_skill(self.store, {"status": "completed"}, generate)
        self.assertEqual(skipped["state"], "skipped")
        self.assertEqual(self.store.index(), [])

    def test_upgrade_activates_the_latest_legacy_candidate_for_its_declared_environment(self):
        for version in ("1.0.0", "1.1.0"):
            self.store.create_candidate(document(version).replace("  - cloud-browser\n", ""))
        with sqlite3.connect(self.store.database) as db:
            db.execute("PRAGMA user_version = 0")

        for _ in range(2):
            store = SkillStore(self.path)
            self.assertEqual(store.active_index("cloud-browser"), [])
            active, = store.active_index("macos-desktop")
            self.assertEqual((active["version"], active["state"], active["evaluations"]), ("1.1.0", "active", {}))
            self.assertTrue(active["available"])
