import unittest
from pathlib import Path
from unittest.mock import Mock

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

    def test_skill_workspace_is_advertised_only_with_bash_and_support(self):
        skills = Mock()
        skills.index.return_value = []
        skills.match.return_value = [{"content": "Check the processed CSV row count."}]
        task = {"task": "Process a CSV file"}

        for flags, use_workspace in [
            ({"bash": True, "skill_workspace": True}, True),
            ({"bash": True}, False),
            ({"bash": False, "skill_workspace": True}, False),
        ]:
            with self.subTest(flags=flags):
                capabilities = {"environment": "cloud-browser", **flags}
                system = build_messages(task, capabilities, skills, [], [], Path.cwd(), 10_000)[0]["content"]
                self.assertIn("## Tool Usage Policy", system)
                self.assertIn("## Skill Awareness", system)
                self.assertEqual(".mini-harness/skills/active/index.json" in system, use_workspace)
                self.assertIn("harness skill manage", system)
                self.assertEqual("Check the processed CSV row count." in system, not use_workspace)
