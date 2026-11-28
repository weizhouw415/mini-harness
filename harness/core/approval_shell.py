"""Recognize read-only shell execution and common commands that require approval."""

import re
import shlex
from collections.abc import Iterator


_PRINTF_FORMAT = re.compile(r"(?:[^%\\]|%[%sd]|\\[\\nt])*")


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


# This is a lexical guardrail for ordinary shell commands, not a shell
# evaluator. Runtime expansion and the effects of arbitrary scripts are unknown.
_SHELL_TOKEN = re.compile(
    r"""#[^\n]*|<<<|<<-?|[;\n&|()]|(?:'[^']*'|"(?:\\.|[^"\\])*"|\\.|[^ \t\r\n;&|()'"\\<]|<(?!<))+""",
    re.DOTALL,
)
_CONTROL_WORDS = {"if", "then", "elif", "else", "while", "until", "do", "!", "{"}
_WRAPPER_OPTIONS = {
    "sudo": {"-u", "-g", "-h", "-p", "-C", "-R", "-D", "-T", "--user", "--group", "--host",
             "--prompt", "--close-from", "--chroot", "--chdir", "--command-timeout"},
    "doas": {"-u", "-C"},
    "env": {"-u", "-C", "--unset", "--chdir"},
    "xargs": {"-a", "-d", "-E", "-I", "-L", "-n", "-P", "-s", "--arg-file", "--delimiter",
              "--eof", "--replace", "--max-lines", "--max-args", "--max-procs", "--max-chars"},
    "exec": {"-a"},
    "command": set(),
    "nohup": set(),
    "busybox": set(),
}
_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
_DISK_DEVICE = re.compile(r"/dev/(?:r?disk|[hsv]d|xvd|nvme|mmcblk|mapper/|dm-|loop)")
_BROAD_RM_TARGETS = frozenset({"/", ".", "./", "..", "../", "*", "./*", "~", "~/", "~/*",
                                "/home", "/home/*", "/Users", "/Users/*", "$HOME", "$HOME/*",
                                "${HOME}", "${HOME}/*"})


def _shell_commands(command: str) -> Iterator[list[str]]:
    """Yield word lists, ignoring comments and literal heredoc bodies."""
    command = command.replace("\\\n", "")
    words = []
    heredocs = []
    heredoc = None
    position = 0
    while token := _SHELL_TOKEN.search(command, position):
        raw = token.group()
        position = token.end()
        if raw in ";\n&|()":
            yield words
            words = []
            if raw == "\n":
                for delimiter, strip_tabs in heredocs:
                    while position < len(command):
                        end = command.find("\n", position)
                        if end == -1:
                            end = len(command)
                        line = command[position:end]
                        position = end + 1
                        if (line.lstrip("\t") if strip_tabs else line) == delimiter:
                            break
                heredocs.clear()
        elif raw.startswith("#"):
            continue
        elif raw in {"<<", "<<-"}:
            heredoc = raw
        else:
            word, = shlex.split(raw)
            if heredoc:
                heredocs.append((word, heredoc == "<<-"))
                heredoc = None
            else:
                words.append(word)
    yield words


def _skip_options(words: list[str], value_options: set[str]) -> list[str]:
    index = 0
    while index < len(words) and words[index].startswith("-") and words[index] != "-":
        option = words[index]
        index += 1 + (option in value_options)
        if option == "--":
            break
    return words[index:]


def _options_and_operands(args: list[str]) -> tuple[set[str], list[str]]:
    options = set()
    operands = []
    parsing = True
    for arg in args:
        if parsing and arg == "--":
            parsing = False
        elif parsing and arg.startswith("--"):
            options.add(arg)
        elif parsing and arg.startswith("-") and len(arg) > 1:
            options.update(arg[1:])
        else:
            operands.append(arg)
    return options, operands


def _find_reason(args: list[str], depth: int) -> str | None:
    index = 0
    while index < len(args):
        arg = args[index]
        index += 1
        if arg in {"-name", "-iname", "-path", "-ipath", "-regex", "-iregex"}:
            index += 1
        elif arg == "-delete":
            return "批量删除匹配的文件，需要确认。"
        elif arg in {"-exec", "-execdir"}:
            end = index
            while end < len(args) and args[end] not in {";", "+"}:
                end += 1
            if depth < 10 and (reason := _destructive_reason(args[index:end], depth + 1)):
                return reason
            index = end + 1
    return None


def _destructive_reason(words: list[str], depth: int) -> str | None:
    while words:
        name = words[0].rsplit("/", 1)[-1]
        args = words[1:]
        if name in _CONTROL_WORDS or _ASSIGNMENT.match(words[0]):
            words = args
        elif name in _WRAPPER_OPTIONS:
            if name == "command" and args and args[0] in {"-v", "-V"}:
                return None
            words = _skip_options(args, _WRAPPER_OPTIONS[name])
        else:
            break
    else:
        return None

    if name == "rm":
        options, operands = _options_and_operands(args)
        recursive = bool(options & {"r", "R", "--recursive"})
        force = bool(options & {"f", "--force"})
        if recursive and (force or _BROAD_RM_TARGETS.intersection(operands)):
            return "递归删除可能造成大量文件丢失，需要确认。"
    elif name == "find":
        return _find_reason(args, depth)
    elif name == "git":
        subcommand = _skip_options(args, {"-C", "-c", "--git-dir", "--work-tree"})
        if subcommand:
            action, *flags = subcommand
            options, _ = _options_and_operands(flags)
            if ((action == "reset" and "--hard" in options) or
                    (action == "clean" and "n" not in options and "--dry-run" not in options
                     and options & {"f", "--force"} and options & {"d", "x", "X"})):
                return "此 Git 命令会丢弃本地文件或更改，需要确认。"
    elif (name == "mkfs" or name.startswith("mkfs.") or name in {"mke2fs", "newfs"}
          or (name == "diskutil" and args and args[0] in
              {"eraseDisk", "eraseVolume", "partitionDisk", "zeroDisk", "secureErase"})
          or (name == "dd" and any(arg.startswith("of=") and _DISK_DEVICE.match(arg[3:]) for arg in args))):
        return "此命令会格式化磁盘或直接写入磁盘设备，需要确认。"
    elif depth < 10:
        if name == "eval":
            return destructive_command_reason(" ".join(args), depth=depth + 1)
        if name in {"bash", "sh", "dash", "zsh", "ksh"}:
            for index, arg in enumerate(args[:-1]):
                if not arg.startswith("-") or arg == "--":
                    break
                if not arg.startswith("--") and "c" in arg[1:]:
                    return destructive_command_reason(args[index + 1], depth=depth + 1)
    return None


def destructive_command_reason(command: str, *, depth: int = 0) -> str | None:
    for words in _shell_commands(command):
        if reason := _destructive_reason(words, depth):
            return reason
    return None
