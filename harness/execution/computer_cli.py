"""Parse the server-routed computer command without invoking a shell."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from urllib.parse import urlsplit

from harness.artifacts.files import workspace_path
from workers.common.actions import SCROLL_DIRECTIONS

COMPUTER_HELP = """Usage: harness computer [--target browser|desktop] COMMAND [ARGS] [--observe]

  screenshot [--output PATH]                 Capture the screen and optionally save it in the workspace.
  observe                                   Capture the current view.
  screen-info                               Show the screen or viewport dimensions.
  click|double-click|right-click X Y          Click a coordinate.
  click|double-click|right-click --selector CSS
  move X Y                                  Move the pointer.
  drag X Y END_X END_Y                       Drag between coordinates.
  type TEXT [--selector CSS]                 Enter text.
  key KEY [--selector CSS]                   Press a named key or shortcut, such as Control+L.
  scroll DIRECTION AMOUNT [--x X --y Y]       Scroll up, down, left, or right by pixels.
  navigate URL                              Navigate the browser to an HTTP or HTTPS URL.
  download X Y [--path PATH]                  Download by clicking a coordinate.
  download --selector CSS [--path PATH]      Download by clicking a browser element.
  upload --selector CSS PATH                 Upload a workspace file through a browser element.

Use --observe after a command to capture the resulting view. Selectors, navigation, and
browser file transfers use the browser target. Omit --target elsewhere to use the run's
default target. Paths are relative to the run workspace. Screenshot output accepts PNG,
JPEG, and WebP. Quote text, selectors, and URLs with shell metacharacters. Submit each
computer command separately. Use -- before a positional argument beginning with --.
"""

_INVOCATION = re.compile(r"\A[ \t]*harness[ \t]+computer(?=$|[^\w./-])")
_POINT_KEYS = ("x", "y", "end_x", "end_y")
_CLICKS = {"click", "double-click", "right-click"}
_BROWSER_ONLY = {"navigate", "download", "upload"}
_OPTIONS = {
    "screenshot": {"output"},
    "click": {"selector"},
    "double-click": {"selector"},
    "right-click": {"selector"},
    "type": {"selector"},
    "key": {"selector"},
    "scroll": {"x", "y"},
    "download": {"selector", "path", "output"},
    "upload": {"selector", "path"},
}
_COMMANDS = {*_OPTIONS, "observe", "screen-info", "move", "drag", "navigate"}
_FLAGS = {"help", "observe"}
_VALUE_OPTIONS = {"target", "selector", "output", "path", "x", "y"}
_LITERAL_ERROR = "Use one literal harness computer command without shell expansions, pipes, or redirects."


@dataclass(frozen=True)
class ComputerCommand:
    action: dict | None
    observe: bool = False
    output: str | None = None


def _literal_words(command: str) -> list[str]:
    words: list[str] = []
    word: list[str] = []
    quote = ""
    started = False
    index = 0
    while index < len(command):
        char = command[index]
        index += 1
        if ord(char) < 32 and char not in "\t\n" or char == "\x7f":
            raise ValueError("Computer commands cannot contain control characters.")
        if char == quote:
            quote = ""
        elif quote == "'":
            word.append(char)
        elif char == "\\":
            if index == len(command) or command[index] in "\n\r":
                raise ValueError(_LITERAL_ERROR)
            if not quote or command[index] in '\\"$`':
                escaped = command[index]
                if (ord(escaped) < 32 and escaped != "\t") or escaped == "\x7f":
                    raise ValueError("Computer commands cannot contain control characters.")
                word.append(escaped)
                index += 1
            else:
                word.append(char)
            started = True
        elif char in "$`" or (not quote and (char in ";|&<>()\n*?[]{}" or (not started and char in "#~"))):
            raise ValueError(_LITERAL_ERROR)
        elif quote:
            word.append(char)
        elif char in "'\"":
            quote = char
            started = True
        elif char in " \t":
            if started:
                words.append("".join(word))
                word.clear()
                started = False
        else:
            word.append(char)
            started = True
    if quote:
        raise ValueError("Close quoted text in the harness computer command.")
    if started:
        words.append("".join(word))
    return words


def _arguments(words: list[str]) -> tuple[list[str], dict[str, str | bool]]:
    values: list[str] = []
    options: dict[str, str | bool] = {}
    tokens = iter(words)
    for token in tokens:
        if token == "--":
            values.extend(tokens)
            break
        if not token.startswith("--"):
            values.append(token)
            continue
        name, equals, value = token[2:].partition("=")
        if name in options:
            raise ValueError(f"Duplicate computer option --{name}.")
        if name in _FLAGS and not equals:
            options[name] = True
        elif name in _VALUE_OPTIONS:
            value = value if equals else next(tokens, "")
            if not value or (not equals and value.startswith("--")):
                raise ValueError(f"Computer option --{name} requires a value.")
            options[name] = value
        else:
            raise ValueError(f"Unknown computer option: {token}.")
    return values, options


def _path(value: str) -> str:
    path = workspace_path(value.removeprefix("./"))
    root = path.split("/", 1)[0].lower()
    if root == ".mini-harness" or root.startswith(".mini-harness-cli-"):
        raise ValueError("Choose a user file in the run workspace.")
    return path


def _number(value: str, name: str, minimum: int = 0) -> int:
    if not re.fullmatch(r"\+?[0-9]{1,6}", value) or not minimum <= int(value) <= 100_000:
        raise ValueError(f"{name} must be an integer from {minimum} to 100000.")
    return int(value)


def _expect(command: str, args: list[str], count: int, syntax: str = "") -> None:
    if len(args) != count:
        raise ValueError(f"Usage: harness computer {command}{' ' + syntax if syntax else ''}.")


def _url(value: str) -> str:
    try:
        url = urlsplit(value)
        if (len(value) <= 8192 and not any(char.isspace() for char in value)
                and url.scheme in {"http", "https"} and url.hostname and not url.username and not url.password):
            return value
    except ValueError:
        pass
    raise ValueError("Navigate requires an HTTP or HTTPS URL without embedded credentials.")


def parse_computer_command(command: str) -> ComputerCommand | None:
    """Return a typed GUI request only for the literal standalone computer CLI."""
    command = command.strip(" \t\r\n")
    if not _INVOCATION.match(command):
        return None
    if len(command.encode("utf-8")) > 128 * 1024:
        raise ValueError("Computer command is too long.")
    words = _literal_words(command)
    if words[:2] != ["harness", "computer"]:
        raise ValueError(_LITERAL_ERROR)
    values, options = _arguments(words[2:])
    if options.pop("help", False) or not values or values == ["help"]:
        return ComputerCommand(action=None)
    name, *args = values
    if name not in _COMMANDS:
        raise ValueError(f"Unknown computer command: {name}. Run harness computer --help.")
    target = options.pop("target", None)
    if target is not None and target not in {"browser", "desktop"}:
        raise ValueError("Computer target must be browser or desktop.")
    observe = bool(options.pop("observe", False))
    extra = options.keys() - _OPTIONS.get(name, set())
    if extra:
        raise ValueError(f"Computer command {name} does not accept --{sorted(extra)[0]}.")

    action: dict = {"action": name.replace("-", "_")}
    selector = options.get("selector")
    if name in _BROWSER_ONLY or selector is not None:
        if target == "desktop":
            raise ValueError("Navigation, selectors, and file transfers require the browser target.")
        target = "browser"
    if target is not None:
        action["target"] = target
    if selector is not None:
        if not selector.strip() or len(selector) > 8192:
            raise ValueError("Selector must be a nonempty string of at most 8192 characters.")
        action["selector"] = selector

    output = None
    if name in {"screenshot", "observe", "screen-info"}:
        _expect(name, args, 0)
        if name in {"screenshot", "observe"} and observe:
            raise ValueError("This computer command already captures the screen.")
        if "output" in options:
            output = _path(options["output"])
            if PurePosixPath(output).suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
                raise ValueError("Screenshot output must end in .png, .jpg, .jpeg, or .webp.")
    elif name in _CLICKS | {"move", "drag", "download"}:
        if selector is not None:
            _expect(name, args, 0, "--selector CSS")
        else:
            count = 4 if name == "drag" else 2
            _expect(name, args, count, "X Y END_X END_Y" if count == 4 else "X Y")
            action.update({key: _number(value, key) for key, value in zip(_POINT_KEYS, args)})
        if name == "download":
            if "path" in options and "output" in options:
                raise ValueError("Choose one option for the download path.")
            destination = options.get("path") or options.get("output")
            if destination:
                action["path"] = _path(destination)
    elif name in {"type", "key"}:
        _expect(name, args, 1, "TEXT" if name == "type" else "KEY")
        value = args[0]
        if (name == "type" and len(value) > 100_000) or (name == "key" and (not value.strip() or len(value) > 80)):
            raise ValueError(f"Invalid computer {name} argument.")
        action["text" if name == "type" else "key"] = value
    elif name == "scroll":
        _expect(name, args, 2, "DIRECTION AMOUNT")
        direction, amount = args
        if direction not in SCROLL_DIRECTIONS:
            raise ValueError("Scroll direction must be up, down, left, or right.")
        action.update(direction=direction, amount=_number(amount, "amount", minimum=1))
        if "x" in options or "y" in options:
            if "x" not in options or "y" not in options:
                raise ValueError("Scroll requires both --x and --y to move the pointer.")
            action.update(x=_number(options["x"], "x"), y=_number(options["y"], "y"))
    elif name == "navigate":
        _expect(name, args, 1, "URL")
        action["url"] = _url(args[0])
    elif name == "upload":
        if selector is None:
            raise ValueError("Upload requires --selector CSS.")
        _expect(name, args, 0 if "path" in options else 1, "--selector CSS PATH")
        action["path"] = _path(options["path"] if "path" in options else args[0])
    return ComputerCommand(action=action, observe=observe, output=output)
