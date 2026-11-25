import json
import unittest

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
