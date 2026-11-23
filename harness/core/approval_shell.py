"""Recognize a conservative, read-only subset of Bash for automatic approval."""

import re
import shlex


# The original rule remains available for Workers that do not isolate PATH.
_LITERAL_COMMAND = re.compile(
    r"""[ \t]*(?:pwd|echo|printf|ls)(?:[ \t]+(?:[A-Za-z0-9_.,:/%+=@-]+|'[^']*'|"[^"$`\\]*"))*[ \t]*"""
)
_LS_OPTION = re.compile(r"-[aAdhlp1]+")
_PRINTF_FORMAT = re.compile(r"(?:[^%\\]|%[%sd]|\\[\\nt])*")


def is_safe_bash_legacy(command: str, *, allow_ls: bool = True) -> bool:
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
    return bool(args and not args[0].startswith("-") and _PRINTF_FORMAT.fullmatch(args[0]))


# Only this syntax is accepted. In particular, no substitutions, variable
# expansions, escapes, subshells, process substitutions or general redirects.
_WORD = re.compile(r'''(?:[A-Za-z0-9_.,:/%+=@~*?\[\]!-]+|'[^']*'|"[^"$`\\]*")+''')
_OPERATORS = ("2>/dev/null", "&&", "||", "|", ";")
_SHORT_OPTIONS = {
    "ls": "aAbBcCdDfFghHiIlLmnopqQrRstTuUxX1",
    "du": "aBcdhHkLlmPsSx",
    "cat": "AbEeNnstTuv",
    "head": "cqvz",
    "tail": "cqvz",
    "wc": "cLlmw",
    "grep": "aAbBcCEeFhHiIlnNoqRsSvwxz",
    "stat": "fFlLnqrsStx",
    "file": "bhiILNnprsv",
    "df": "aBghHiklmPTt",
    "ps": "aAdefFlLopTuUwx",
    "whoami": "",
    "uname": "aimnoprsv",
}
# A few flags consume an argument. Restrict numerical arguments for those that
# take a count; grep's pattern flag is handled separately.
_NUMBER_OPTIONS = {"head": {"-n", "-c"}, "tail": {"-n", "-c"}, "du": {"-d"}}


def _tokens(command: str) -> list[tuple[str, str]] | None:
    tokens = []
    pos = 0
    while pos < len(command):
        if command[pos] in " \t":
            pos += 1
            continue
        op = next((op for op in _OPERATORS if command.startswith(op, pos)), None)
        if op:
            tokens.append((op, op))
            pos += len(op)
            continue
        match = _WORD.match(command, pos)
        if not match:
            return None
        raw = match.group()
        # Quotes are retained in raw to distinguish literal glob patterns.
        try:
            parsed = shlex.split(raw)
        except ValueError:
            return None
        if len(parsed) != 1:
            return None
        tokens.append((parsed[0], raw))
        pos = match.end()
    return tokens


def _unquoted_glob(raw: str) -> bool:
    quote = None
    for char in raw:
        if quote:
            if char == quote:
                quote = None
        elif char in {"'", '"'}:
            quote = char
        elif char in "*?[":
            return True
    return False


def _safe_find(args: list[tuple[str, str]]) -> bool:
    index = 0
    while index < len(args):
        value, raw = args[index]
        if value in {"-maxdepth", "-mindepth"}:
            index += 1
            if index == len(args) or not args[index][0].isdigit():
                return False
        elif value in {"-name", "-iname", "-path", "-ipath", "-type"}:
            index += 1
            if index == len(args):
                return False
            arg, arg_raw = args[index]
            if value == "-type" and arg not in {"b", "c", "d", "f", "l", "p", "s"}:
                return False
            # An unquoted wildcard can expand into find predicates such as -delete.
            if _unquoted_glob(arg_raw):
                return False
        elif value in {"-print", "-print0", "-empty", "-true", "-false", "-a", "-o", "!"}:
            pass
        elif value.startswith("-") or value == "!":
            return False
        elif _unquoted_glob(raw) and not raw.startswith(("/", "./", "~/")):
            return False
        index += 1
    return True


def _safe_command(tokens: list[tuple[str, str]], allow_ls: bool) -> bool:
    if not tokens:
        return False
    name, raw_name = tokens[0]
    # A command name may not be quoted or expanded.
    if name != raw_name:
        return False
    args = tokens[1:]
    if name == "pwd":
        return all(arg in {"-L", "-P"} for arg, _ in args)
    if name == "echo":
        return True
    if name == "printf":
        values = [arg for arg, _ in args]
        if values and values[0] == "--":
            values = values[1:]
            args = args[1:]
        return bool(values and not values[0].startswith("-") and
                    not any(ch in args[0][1] for ch in "*?[") and _PRINTF_FORMAT.fullmatch(values[0]))
    if name == "find":
        return _safe_find(args)
    if name not in _SHORT_OPTIONS or (name == "ls" and not allow_ls):
        return False
    index = 0
    options = True
    while index < len(args):
        arg, _ = args[index]
        if options and arg == "--":
            options = False
        elif name == "file" and options and _unquoted_glob(args[index][1]) and not args[index][1].startswith(("/", "./", "~/")):
            # file -C writes a compiled magic database; an unprefixed glob
            # can otherwise introduce that option through a filename.
            return False
        elif options and arg.startswith("-") and arg != "-":
            if arg in _NUMBER_OPTIONS.get(name, set()):
                index += 1
                if index == len(args) or not args[index][0].isdigit():
                    return False
            elif name in {"head", "tail"} and re.fullmatch(r"-[nc]\d+", arg):
                pass
            elif name == "du" and re.fullmatch(r"-d\d+", arg):
                pass
            elif name == "grep" and arg in {"-e", "-A", "-B", "-C"}:
                index += 1
                if index == len(args) or (arg != "-e" and not args[index][0].isdigit()):
                    return False
            elif arg.startswith("--") or not all(ch in _SHORT_OPTIONS[name] for ch in arg[1:]):
                return False
        index += 1
    return True


def is_safe_bash(command: str, *, allow_ls: bool = True) -> bool:
    """Recognize read-only commands, provided the Worker enforces a safe PATH."""
    if len(command) > 4096 or any(not (ch.isprintable() or ch == "\t") for ch in command):
        return False
    tokens = _tokens(command)
    if not tokens:
        return False
    current: list[tuple[str, str]] = []
    redirected = False
    for value, raw in tokens:
        if value == raw and value == "2>/dev/null":
            if not current or redirected:
                return False
            redirected = True
        elif value == raw and value in {"&&", "||", "|", ";"}:
            if not _safe_command(current, allow_ls):
                return False
            current = []
            redirected = False
        else:
            # Redirection is only supported at the end of a command.
            if redirected:
                return False
            current.append((value, raw))
    return _safe_command(current, allow_ls)
