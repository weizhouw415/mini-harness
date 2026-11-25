import unittest
from pathlib import Path

from harness.core.context import TOOLS, build_messages


class ToolContextTest(unittest.TestCase):
    def test_model_sees_only_bash_and_discovers_gui_through_cli(self):
        schemas = {tool["function"]["name"]: tool["function"]["parameters"] for tool in TOOLS}
        self.assertEqual(set(schemas), {"bash"})
        bash = schemas["bash"]
        self.assertEqual(bash["required"], ["command"])
        self.assertEqual(bash["properties"]["timeout"]["default"], 120)
        self.assertFalse(bash["additionalProperties"])
        self.assertIn("harness computer", TOOLS[0]["function"]["description"])

    def test_system_prompt_contains_skill_profiles_only(self):
        profiles = [{"name": "csv", "description": "Process CSV", "path": "/skills/csv/SKILL.md"}]
        system = build_messages({"task": "Process a CSV file"}, {"bash": True}, profiles,
                                [], [], Path.cwd(), 10_000, skill_directory="/skills")[0]["content"]
        self.assertIn("## Tool Usage Policy", system)
        self.assertIn("Process CSV", system)
        self.assertIn("/skills/csv/SKILL.md", system)
        self.assertNotIn("harness skill", system)
