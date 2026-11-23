import unittest

from harness.core.approval_shell import is_safe_bash


class BashAutoApprovalTest(unittest.TestCase):
    def test_approves_literal_output_and_listing_the_current_directory(self):
        for command in (
            "pwd",
            " \tpwd -P\t",
            'echo "你好，世界"',
            "echo 'literal $HOME; # comment'",
            r"printf '%s\n' 'two words'",
            r"printf -- '%d %s %%\t' 25 done",
            "ls",
            "ls -alh -1",
        ):
            with self.subTest(command=command):
                self.assertTrue(is_safe_bash(command))

    def test_requires_approval_for_shell_syntax_with_side_effects(self):
        for command in (
            "echo hello; touch item",
            "echo hello\ntouch item",
            "echo hello && touch item",
            "echo hello | tee item",
            "echo hello > item",
            "echo $(pwd)",
            'echo "`pwd`"',
            'printf "%s" "$HOME"',
            "echo <(pwd)",
            "echo ~/item",
            "echo ./*",
            "echo {one,two}",
            r"e\cho hello",
            "MODE=test echo hello",
            "echo 'unterminated",
            "echo hello # unsupported syntax",
            "echo hello\x00",
        ):
            with self.subTest(command=command):
                self.assertFalse(is_safe_bash(command))

    def test_requires_approval_for_unknown_commands_and_risky_arguments(self):
        for command in (
            "cat report.txt",
            "ls /",
            "ls ../other-run",
            "ls -R",
            "ls -L",
            "ls --color=always",
            "bash -c 'pwd'",
            "pwd unexpected",
            "printf -v value '%s' data",
            "printf '%n' value",
            "printf '%999999999s' data",
            "printf '' " + "a" * 4096,
        ):
            with self.subTest(command=command):
                self.assertFalse(is_safe_bash(command))
