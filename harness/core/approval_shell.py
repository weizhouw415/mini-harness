"""Recognize the small subset of Bash suitable for automatic approval."""

import re
import shlex


# Each word is wholly unquoted, single quoted, or double quoted. Double quotes
# exclude every expansion and escape character. Bash and shlex agree on the
# meaning of this subset, and the actual command is still run by Bash.
_LITERAL_COMMAND = re.compile(
    r"""[ \t]*(?:pwd|echo|printf|ls)(?:[ \t]+(?:[A-Za-z0-9_.,:/%+=@-]+|'[^']*'|"[^"$`\\]*"))*[ \t]*"""
)
_LS_OPTION = re.compile(r"-[aAdhlp1]+")
_PRINTF_FORMAT = re.compile(r"(?:[^%\\]|%[%sd]|\\[\\nt])*")


def is_safe_bash(command: str, *, allow_ls: bool = True) -> bool:
    """Allow literal output and inspection of the current workspace directory."""
    if len(command) > 4096 or any(not (ch.isprintable() or ch == "\t") for ch in command):
        return False
    if not _LITERAL_COMMAND.fullmatch(command):
        return False

    name, *args = shlex.split(command)
    if name == "pwd":
        return all(arg in {"-L", "-P"} for arg in args)
    if name == "echo":
        return True
    if name == "ls":
        return allow_ls and all(_LS_OPTION.fullmatch(arg) for arg in args)

    if args and args[0] == "--":
        args = args[1:]
    # Restrict formats so they cannot request large widths or modify variables.
    return bool(args and not args[0].startswith("-") and _PRINTF_FORMAT.fullmatch(args[0]))
