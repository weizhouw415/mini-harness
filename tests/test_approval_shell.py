import unittest

from harness.core.approval_shell import destructive_command_reason, is_safe_bash


class ReadOnlyBashTest(unittest.TestCase):
    def test_recognizes_common_read_only_commands(self):
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

    def test_rejects_shell_syntax_with_side_effects(self):
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

    def test_rejects_commands_and_options_with_side_effects(self):
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


class DestructiveBashTest(unittest.TestCase):
    def test_recognizes_high_risk_commands_in_common_shell_forms(self):
        for command in (
            "rm -rf -- /",
            "echo done && sudo -u root /bin/rm -f -R -- 'old project'",
            "echo before # comment\nrm --force --recursive removed",
            "bash -lc 'env MODE=test rm -fR removed'",
            "if test -d folder; then rm -r -f folder; fi",
            "rm -\\\nrf removed",
            "rm -r -- \"$HOME\"",
            r"find . -type d -exec rm -rf {} \;",
            "find . -name '*.tmp' -delete",
            "find . -type d -print0 | xargs -0 rm -rf",
            "git -C working-copy reset --hard",
            "git clean -xfd",
            "diskutil eraseDisk APFS New /dev/disk2",
            "mkfs.ext4 /dev/sda",
            "dd if=archive.img of=/dev/disk2",
        ):
            with self.subTest(command=command):
                self.assertIsNotNone(destructive_command_reason(command))

    def test_allows_routine_edits_and_literal_command_text(self):
        for command in (
            "printf done > report.txt",
            "rm -f temporary.txt",
            "rm -r -- -f",
            "grep 'rm -rf /' README.md",
            "printf '%s' rm -rf /",
            "cat > notes.txt <<'EOF'\nrm -rf /\nEOF",
            r"find . -exec echo -delete \;",
            "git clean -ndf",
            "dd if=archive.img of=/dev/null",
        ):
            with self.subTest(command=command):
                self.assertIsNone(destructive_command_reason(command))
