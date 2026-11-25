import json
import unittest
import tempfile
from pathlib import Path

from harness.skills.evolution import review_skills


class SkillReviewTests(unittest.IsolatedAsyncioTestCase):
    async def test_review_samples_middle_and_failures_and_redacts(self):
        events = []
        for i in range(80):
            events += [{"type": "tool_call", "data": {"call_id": str(i), "arguments": {"command": f"step {i}"}}},
                       {"type": "tool_result", "data": {"call_id": str(i), "result": {"exit_code": 1 if i == 39 else 0, "stdout": "api_key=abc123"}}}]
        captured = []
        async def complete(messages, tools):
            captured.append(messages)
            return {"content": json.dumps({"action": "create", "name": "comic-slides", "content": "---\nname: comic-slides\ndescription: Create illustrated slides\n---\n# Steps\nDraw and verify each slide."})}
        result = await review_skills({"status": "completed", "task": "make slides", "events": events}, [], complete, environment="macos-desktop")
        self.assertEqual(result["name"], "comic-slides")
        self.assertIn('step 39', captured[0][1]["content"])
        self.assertNotIn('abc123', captured[0][1]["content"])
        self.assertLess(len(captured[0][1]["content"]), 61000)

    async def test_rejects_invalid_action_and_sensitive_document(self):
        async def reply(messages, tools):
            return {"content": json.dumps({"action": "update", "name": "no-such-skill", "content": "---\nname: no-such-skill\ndescription: Test\n---\nbody"})}
        with self.assertRaises(ValueError):
            await review_skills({"status": "completed"}, [], reply)
        async def sensitive(messages, tools):
            return {"content": json.dumps({"action": "create", "name": "example", "content": "---\nname: example\ndescription: Test\n---\napi_key=secret123"})}
        with self.assertRaises(ValueError):
            await review_skills({"status": "completed"}, [], sensitive)

    async def test_failed_run_skips_without_model_call(self):
        async def reply(*args):
            self.fail("Must not call model")
        self.assertEqual((await review_skills({"status": "failed"}, [], reply))["status"], "skipped")

    async def test_review_reads_files_and_returns_update_preimages(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "example"
            (root / "references").mkdir(parents=True)
            original = "---\nname: example\ndescription: Test\n---\nOriginal steps"
            (root / "SKILL.md").write_text(original)
            (root / "references" / "details.md").write_text("original reference")
            calls = 0

            async def complete(messages, tools):
                nonlocal calls
                calls += 1
                if calls == 1:
                    return {"reasoning_content": "private", "tool_calls": [
                        {"id": "one", "type": "function", "function": {
                            "name": "read_skill_file", "arguments": json.dumps({"name": "example"})}},
                        {"id": "two", "type": "function", "function": {
                            "name": "read_skill_file", "arguments": json.dumps({"name": "example", "path": "references/details.md"})}},
                    ]}
                self.assertNotIn("reasoning_content", messages[-3])
                self.assertIn("original reference", messages[-1]["content"])
                return {"content": json.dumps({"action": "update", "name": "example",
                    "content": original + "\nUpdated", "files": {"references/details.md": "new reference"}})}

            profile = {"name": "example", "description": "Test", "path": str(root / "SKILL.md")}
            result = await review_skills({"status": "completed"}, [profile], complete)
            self.assertEqual(result["base_content"], original)
            self.assertEqual(result["files"]["references/details.md"],
                             {"content": "new reference", "base_content": "original reference"})
