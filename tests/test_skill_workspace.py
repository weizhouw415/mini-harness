import json
import os
import tempfile
import unittest
from pathlib import Path

from harness.skills import SkillStore, evaluate_skill
from harness.skills.loader import MAX_SKILL_BYTES
from tests.test_skills import document
from workers.common.skill_workspace import collect_skill_candidates, prepare_skill_workspace


class SkillWorkspaceTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.workspace = self.directory / "run"
        self.workspace.mkdir()
        self.skills = SkillStore(self.directory / "catalog")
        self.staging = self.workspace / ".mini-harness" / "skills"

    async def test_only_verified_active_skills_are_seeded_and_drafts_remain_candidates(self):
        async def verify(skill, environment):
            return {"passed": True}

        self.skills.create_candidate(document())
        self.assertEqual(self.skills.workspace_snapshot("cloud-browser", "CSV"), [])
        await evaluate_skill(self.skills, "csv-roundtrip", "1.0.0", "cloud-browser", verify)
        self.skills.publish("csv-roundtrip", "1.0.0", approved=True)
        self.assertEqual(self.skills.workspace_snapshot("macos-desktop", "CSV"), [])

        prepare_skill_workspace(self.workspace, self.skills.workspace_snapshot("cloud-browser", "CSV"))
        active = self.staging / "active"
        self.assertEqual(json.loads((active / "index.json").read_bytes())[0]["version"], "1.0.0")
        self.assertEqual((active / "csv-roundtrip" / "SKILL.md").read_text(encoding="utf-8"), document())

        draft = self.staging / "candidates" / "csv-roundtrip" / "SKILL.md"
        draft.parent.mkdir()
        draft.write_text(document("1.1.0"), encoding="utf-8")
        collected = collect_skill_candidates(self.workspace)
        self.assertEqual(collected["rejected"], 0)
        for _ in range(2):
            self.assertEqual(self.skills.import_workspace_candidates(collected["candidates"]), {
                "accepted": 1, "rejected": 0,
            })
        self.assertEqual(self.skills.get("csv-roundtrip", "1.1.0")["state"], "candidate")
        self.assertEqual(self.skills.get("csv-roundtrip")["version"], "1.0.0")
        self.assertEqual(self.skills.import_workspace_candidates([
            {"name": "wrong-name", "content": document("2.0.0")},
            {"name": "csv-roundtrip", "content": document("1.1.0") + "\nChanged content\n"},
            {
                "name": "csv-roundtrip",
                "content": document("2.0.0").replace("下载 CSV 后处理并上传", '"\\uD800"'),
            },
        ]), {"accepted": 0, "rejected": 3})

    @unittest.skipUnless(os.name == "posix", "Uses POSIX links and FIFOs")
    def test_replaced_candidate_parent_links_pipes_and_oversize_files_are_rejected(self):
        prepare_skill_workspace(self.workspace, [])
        external = self.directory / "external"
        external.mkdir()
        outside_document = external / "SKILL.md"
        outside_document.write_text(document(), encoding="utf-8")
        candidates = self.staging / "candidates"
        (candidates / "symlink-dir").symlink_to(external, target_is_directory=True)
        for name in ("symlink-file", "hardlink-file", "pipe", "oversized", "valid"):
            (candidates / name).mkdir()
        (candidates / "symlink-file" / "SKILL.md").symlink_to(outside_document)
        os.link(outside_document, candidates / "hardlink-file" / "SKILL.md")
        os.mkfifo(candidates / "pipe" / "SKILL.md")
        (candidates / "oversized" / "SKILL.md").write_bytes(b"x" * (MAX_SKILL_BYTES + 1))
        (candidates / "valid" / "SKILL.md").write_text("valid content", encoding="utf-8")
        self.assertEqual(collect_skill_candidates(self.workspace), {
            "candidates": [{"name": "valid", "content": "valid content"}], "rejected": 5,
        })
        candidates.rename(candidates.with_name("original-candidates"))
        candidates.symlink_to(external, target_is_directory=True)
        self.assertEqual(collect_skill_candidates(self.workspace), {"candidates": [], "rejected": 1})
