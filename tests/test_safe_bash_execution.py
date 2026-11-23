import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from workers.desktop.worker import Run, WorkerBackend


class SafeBashExecutionTest(unittest.IsolatedAsyncioTestCase):
    async def test_windows_shells_use_the_restricted_path_for_read_commands(self):
        backend = WorkerBackend()
        with patch("workers.desktop.worker.sys.platform", "win32"):
            with patch.dict(os.environ, {"MINI_DESKTOP_BASH_KIND": "git-bash", "MINI_DESKTOP_BASH_PATH": "bash.exe"}):
                args = await backend._bash_arguments(Path("workspace"), "ls -la")
            self.assertEqual(args, ["bash.exe", "--noprofile", "--norc", "-p", "-c", "PATH=/usr/bin:/bin\nls -la"])
            with patch.dict(os.environ, {"MINI_DESKTOP_BASH_KIND": "wsl", "MINI_DESKTOP_BASH_PATH": "wsl.exe"}):
                with patch("workers.desktop.worker.run_shell", AsyncMock(return_value={"stdout": "/workspace\n", "exit_code": 0})):
                    args = await backend._bash_arguments(Path("workspace"), "ls -la")
            self.assertEqual(args, ["wsl.exe", "--cd", "/workspace", "--exec", "/bin/bash", "--noprofile", "--norc", "-p", "-c", "PATH=/usr/bin:/bin\nls -la"])

    @unittest.skipIf(os.name == "nt", "uses POSIX shell scripts")
    async def test_read_only_command_cannot_use_a_spoofed_path_executable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake_bin = root / "fake-bin"
            fake_bin.mkdir()
            marker = root / "executed"
            fake_ls = fake_bin / "ls"
            fake_ls.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
            fake_ls.chmod(0o755)
            (root / "visible.txt").write_text("content")
            with patch.dict(os.environ, {"MINI_DESKTOP_BASH": "1", "PATH": str(fake_bin)}):
                backend = WorkerBackend(root)
                backend.check_available = lambda: None
                backend._runs["run"] = Run(root)
                result = await backend.bash("run", "ls -la", 10)
            self.assertEqual(result["exit_code"], 0, result)
            self.assertIn("visible.txt", result["stdout"])
            self.assertFalse(marker.exists())

    @unittest.skipIf(os.name == "nt", "uses POSIX shell scripts")
    async def test_other_commands_keep_the_user_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            helper = root / "helper"
            helper.write_text("#!/bin/sh\nprintf custom-helper")
            helper.chmod(0o755)
            with patch.dict(os.environ, {"MINI_DESKTOP_BASH": "1", "PATH": str(root)}):
                backend = WorkerBackend(root)
                backend.check_available = lambda: None
                backend._runs["run"] = Run(root)
                result = await backend.bash("run", "helper", 10)
            self.assertEqual(result["exit_code"], 0, result)
            self.assertEqual(result["stdout"], "custom-helper")
