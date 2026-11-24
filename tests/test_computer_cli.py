import unittest

from harness.execution.computer_cli import ComputerCommand, parse_computer_command


class ComputerCliParserTest(unittest.TestCase):
    def test_only_standalone_leading_command_is_routed(self):
        for command in ("harness view figure.png", "harness computerize", "echo 'harness computer'", "pwd"):
            with self.subTest(command=command):
                self.assertIsNone(parse_computer_command(command))
        for command in ("harness computer", "harness computer help", "harness computer --help"):
            with self.subTest(command=command):
                self.assertEqual(parse_computer_command(command), ComputerCommand(action=None))
        self.assertEqual(parse_computer_command("\n  harness computer screenshot\r\n"),
                         ComputerCommand(action={"action": "screenshot"}))

    def test_preserves_quoted_arguments_and_text_containing_shell_operators(self):
        command = "harness computer --target browser type 'hello $name; a & b\nnext' --selector '#target' --observe"
        self.assertEqual(parse_computer_command(command), ComputerCommand({
            "action": "type", "target": "browser", "text": "hello $name; a & b\nnext", "selector": "#target",
        }, observe=True))
        self.assertEqual(parse_computer_command("harness computer type -- '--hello'").action["text"], "--hello")
        self.assertEqual(parse_computer_command(r'harness computer type "\$5"').action["text"], "$5")
        self.assertEqual(parse_computer_command("harness computer type ''").action["text"], "")

    def test_compound_commands_and_expansions_are_rejected(self):
        for suffix in ("; pwd", " && pwd", "\npwd", " | pwd", " >out", " $(pwd)", ' "$(pwd)"', " `pwd`"):
            with self.subTest(suffix=suffix), self.assertRaises(ValueError):
                parse_computer_command("harness computer screenshot" + suffix)
        with self.assertRaises(ValueError):
            parse_computer_command("harness computer type 'unterminated")

    def test_action_metadata_and_selector_target(self):
        self.assertEqual(parse_computer_command("harness computer double-click 17 3 --observe"),
                         ComputerCommand({"action": "double_click", "x": 17, "y": 3}, observe=True))
        self.assertEqual(parse_computer_command("harness computer screenshot --output './screens/View.WEBP'"),
                         ComputerCommand({"action": "screenshot"}, output="screens/View.WEBP"))
        self.assertEqual(parse_computer_command("harness computer download --selector '#file' --path 'docs/x.csv'"),
                         ComputerCommand({"action": "download", "target": "browser", "selector": "#file",
                                          "path": "docs/x.csv"}))
        self.assertEqual(parse_computer_command("harness computer upload --selector 'input[type=file]' 'a b.txt'"),
                         ComputerCommand({"action": "upload", "target": "browser", "selector": "input[type=file]",
                                          "path": "a b.txt"}))
        self.assertEqual(parse_computer_command("harness computer scroll down 20 --x 8 --y 7").action,
                         {"action": "scroll", "direction": "down", "amount": 20, "x": 8, "y": 7})
        self.assertEqual(parse_computer_command("harness computer screen-info").action, {"action": "screen_info"})

    def test_rejects_invalid_data_for_each_action(self):
        for arguments in (
            "screenshot --output ../x.png", "screenshot --output /tmp/x.png", "screenshot --output file.html",
            "screenshot --output .mini-harness/x.png", "observe --output a.png", "drag 1 2 3", "click 1 2 --x 1",
            "click 1 2 --selector foo", "scroll down 1 --x 2", "scroll up 0", "navigate file:///tmp/path",
            "navigate https://user:pass@example.test", "download --selector foo --target desktop",
            "upload --selector foo a --path b", "move 100001 2",
        ):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                parse_computer_command("harness computer " + arguments)
