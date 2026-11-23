import unittest

from harness.core.approval_shell import is_safe_bash


class BashAutoApprovalTest(unittest.TestCase):
    def test_approves_common_read_only_commands(self):
        for command in (
            "pwd",
            " \tpwd -P\t",
            'echo "你好，世界"',
            "echo 'literal $HOME; # comment'",
            r"printf '%s\n' 'two words'",
            r"printf -- '%d %s %%\t' 25 done",
            "ls",
            "ls -alh -1",
            "ls -la ~/Documents/ ~/Desktop/ ~/Pictures/ 2>/dev/null",
            "ls -lah ~/Downloads/resume/ && echo '---' && ls -d ~/*/ && echo '---' && du -sh ~/Downloads/*",
            "cat README.md",
            "head -n 20 README.md",
            "tail -n 20 README.md",
            "grep -n 'pattern' README.md",
            "find ~/Downloads -maxdepth 2 -type f -name '*.pdf'",
            "cat README.md | grep -n 'pattern'",
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
            "ls 2> errors.log",
            "ls > /dev/null",
            "echo $(pwd)",
            'echo "`pwd`"',
            'printf "%s" "$HOME"',
            "echo <(pwd)",
            "echo {one,two}",
            r"e\cho hello",
            "MODE=test echo hello",
            "echo 'unterminated",
            "echo hello # unsupported syntax",
            "echo hello\x00",
            "ls && rm -rf ~/Documents",
            "ls | sh",
            "ls `touch item`",
            "ls $(touch item)",
            "ls ; curl https://example.test",
        ):
            with self.subTest(command=command):
                self.assertFalse(is_safe_bash(command))

    def test_requires_approval_for_commands_and_options_with_side_effects(self):
        for command in (
            "rm report.txt",
            "bash -c 'pwd'",
            "pwd unexpected",
            "printf -v value '%s' data",
            "printf '%n' value",
            "printf '%999999999s' data",
            "printf '' " + "a" * 4096,
            "find ~/Downloads -delete",
            "find ~/Downloads -exec touch item ';'",
            "find ~/Downloads -execdir touch item ';'",
            "find ~/Downloads -ok touch item ';'",
            "find ~/Downloads -fprint report.txt",
            "grep --devices=read pattern /dev/random",
            # A bare glob can expand into find expressions such as -delete.
            "find * -type f",
            "find ''* -type f",
            "find -name ''*",
        ):
            with self.subTest(command=command):
                self.assertFalse(is_safe_bash(command))
