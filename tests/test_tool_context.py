import unittest
from pathlib import Path
from unittest.mock import Mock

from harness.core.context import TOOLS, build_messages


class ToolContextTest(unittest.TestCase):
    def test_model_can_use_bash_and_canonical_gui_actions(self):
        schemas = {tool["function"]["name"]: tool["function"]["parameters"] for tool in TOOLS}
        self.assertEqual(set(schemas), {"bash", "computer_use"})
        bash = schemas["bash"]
        self.assertEqual(bash["required"], ["command"])
        self.assertEqual(bash["properties"]["timeout"]["default"], 120)
        self.assertFalse(bash["additionalProperties"])

        computer = schemas["computer_use"]
        self.assertEqual(computer["required"], ["action"])
        self.assertFalse(computer["additionalProperties"])
        fields = computer["properties"]
        actions = {"double_click", "right_click", "move", "drag", "key", "press"}
        self.assertTrue(actions.issubset(fields["action"]["enum"]))
        self.assertEqual(set(fields["direction"]["enum"]), {"up", "down", "left", "right"})

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
                self.assertEqual(".mini-harness/skills/candidates/<name>/SKILL.md" in system, use_workspace)
                self.assertEqual("Check the processed CSV row count." in system, not use_workspace)
