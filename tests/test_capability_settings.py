import tempfile
import unittest
import os
from itertools import product
from pathlib import Path
from unittest.mock import Mock

import httpx

from harness.api.server import create_app
from harness.config import Settings
from harness.core.context import build_messages, build_tools
from workers.common.transport import _Runtime
from workers.desktop.worker import WorkerBackend


HEADERS = {"Sec-Fetch-Site": "same-origin", "X-Harness-UI": "1"}
ALL_ON = {"view": True, "register": True, "computer_use": True}


class CapabilitySettingsTest(unittest.IsolatedAsyncioTestCase):
    async def test_settings_are_local_validated_and_persisted(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(data_dir=Path(directory), api_token="token", worker_secret="secret")
            app = create_app(settings, local_ui=True)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://127.0.0.1") as client:
                self.assertEqual((await client.get("/ui/capabilities")).status_code, 403)
                self.assertEqual((await client.put("/ui/capabilities", json=ALL_ON)).status_code, 403)
                self.assertEqual((await client.get("/ui/capabilities", headers=HEADERS)).json(), ALL_ON)
                for invalid in ({**ALL_ON, "view": "false"}, {**ALL_ON, "extra": True}, {"view": False}):
                    self.assertEqual((await client.put("/ui/capabilities", headers=HEADERS,
                                                       json=invalid)).status_code, 422)
                disabled = {name: False for name in ALL_ON}
                result = (await client.put("/ui/capabilities", headers=HEADERS,
                                           json=disabled)).raise_for_status()
                self.assertEqual(result.json(), disabled)
                self.assertEqual(result.headers.get("cache-control"), "no-store")

            app = create_app(settings, local_ui=True)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://127.0.0.1") as client:
                self.assertEqual((await client.get("/ui/capabilities", headers=HEADERS)).json(), disabled)


class CapabilityPromptTest(unittest.TestCase):
    def test_individual_switches_control_prompts_and_tool_schema(self):
        worker = {"environment": "cloud-browser", "bash": True, "artifact_workspace": True,
                  "computer": True, "computer_cli": True, "actions": ["screenshot"],
                  "computer_targets": {"browser": ["screenshot"]}, "cli_capability_toggles": True}
        skills = Mock()
        skills.index.return_value = []
        skills.match.return_value = []
        for view, register, computer in product((False, True), repeat=3):
            enabled = {"view": view, "register": register, "computer_use": computer}
            with self.subTest(enabled=enabled):
                prompt = build_messages({"task": "test"}, worker, skills, [], [], Path.cwd(), 10_000,
                                        cli_capabilities=enabled)[0]["content"]
                tools = build_tools(enabled)
                self.assertEqual([entry["function"]["name"] for entry in tools], ["bash"])
                self.assertEqual("harness view" in prompt, view)
                self.assertEqual("harness register" in prompt, register)
                self.assertEqual("harness computer" in prompt, computer)
                self.assertEqual("harness view" in str(tools), view)
                self.assertEqual("harness computer" in str(tools), computer)
                self.assertIn("Uploaded files are available", prompt)
                if not computer:
                    self.assertNotIn('"computer"', prompt)
                    self.assertNotIn('"computer_cli"', prompt)
                    self.assertNotIn('"computer_targets"', prompt)
                    self.assertNotIn('"screenshot"', prompt)

    def test_disabled_commands_are_not_reintroduced_by_dynamic_system_context(self):
        worker = {"environment": "cloud-browser", "bash": True, "artifact_workspace": True}
        skills = Mock()
        skills.index.return_value = [
            {"name": "visual", "state": "active", "description": "Use harness view to inspect"},
            {"name": "data", "state": "active", "description": "Process CSV"},
        ]
        skills.match.return_value = [{"content": "Run harness computer screenshot"},
                                     {"content": "Validate the CSV row count."}]
        events = [
            {"type": "assistant", "data": {"message": {"role": "assistant", "content": ""},
                                             "calls": [{"call_id": "1", "provider_id": "p1"}]}},
            {"type": "tool_result", "data": {"call_id": "1", "result": {
                "error": "harness register failed", "path": "harness view results.txt"}}},
        ]
        prompt = build_messages({"task": "test"}, worker, skills, [], events, Path.cwd(), 10_000,
                                cli_capabilities={name: False for name in ALL_ON})[0]["content"]
        for command in ("harness view", "harness register", "harness computer"):
            self.assertNotIn(command, prompt)
        self.assertIn("Process CSV", prompt)
        self.assertIn("Validate the CSV row count", prompt)


@unittest.skipIf(os.name == "nt", "Uses a POSIX desktop Bash executable")
class CapabilityWorkerTest(unittest.IsolatedAsyncioTestCase):
    async def test_shell_injection_and_artifact_requests_obey_independent_switches(self):
        with tempfile.TemporaryDirectory() as directory:
            backend = WorkerBackend(Path(directory))
            backend.bash_enabled = True
            backend.check_available = lambda: None
            runtime = _Runtime(backend)
            await runtime.execute("open", "run.open", {"run_id": "run-1", "workspace_id": "ws-1"})
            common = {"run_id": "run-1", "timeout": 5}
            flags = {"view": False, "register": True, "computer_use": False}
            result = await runtime.execute("first", "bash.execute", {**common, "cli_capabilities": flags,
                "command": "printf hello > item.txt; harness view item.txt; harness register item.txt"})
            self.assertEqual(result["exit_code"], 0)
            self.assertEqual([block["type"] for block in result["content"]], ["artifact"])
            self.assertEqual(result["content"][0]["data"], b"hello")
            help_result = await runtime.execute("help", "bash.execute", {**common, "cli_capabilities": flags,
                "command": "harness --help"})
            self.assertIn("harness register", help_result["stdout"])
            self.assertNotIn("harness view", help_result["stdout"])
            self.assertNotIn("harness computer", help_result["stdout"])
            # Even a manually forged request cannot make a disabled command export a preview.
            forged = await runtime.execute("forged", "bash.execute", {**common, "cli_capabilities": flags,
                "command": "printf '%s\\0' 2 \"$MINI_HARNESS_RUN_ROOT\" \"$PWD\" view item.txt >> \"$MINI_HARNESS_ARTIFACT_REQUESTS\""})
            self.assertFalse(any(block.get("type") == "image" for block in forged.get("content", [])))
            self.assertIn("disabled", str(forged.get("content", [])))
            off = {name: False for name in ALL_ON}
            shell = await runtime.execute("bash-only", "bash.execute", {**common, "cli_capabilities": off,
                "command": "test -z \"${MINI_HARNESS_ARTIFACT_REQUESTS:-}\" && printf bash-only"})
            self.assertEqual(shell["stdout"], "bash-only")
            self.assertEqual(shell["exit_code"], 0)
            await runtime.close()
