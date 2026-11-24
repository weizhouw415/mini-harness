"""Handle server-side Skill commands sent through the Bash tool."""

from __future__ import annotations

import json
import re
import shlex
import sqlite3

from .loader import SkillStore

SKILL_COMMAND_GUIDANCE = """Run each Skill command as a standalone Bash tool call.
Use `harness skill list` to discover available skills and `harness skill view NAME` to read one.
Use `harness skill view NAME PATH` to read a supporting file. Paths are relative to the skill.

Use `harness skill manage '<JSON object>'` to save a durable workflow or improve one already listed.
You can also pass JSON as a literal heredoc with `harness skill manage <<'JSON'` on the first line
and `JSON` alone on the last line. In JSON strings, represent line breaks with `\\n`.

- Create: `{"action":"create","name":"example","content":"---\\nname: example\\ndescription: When to use this workflow\\n---\\nReusable instructions"}`
- Patch: `{"action":"patch","name":"example","base_version":"0.1.0","old_string":"Original step","new_string":"Better step"}`
- Rewrite: `{"action":"edit","name":"example","base_version":"0.1.0","content":"full SKILL.md"}`
- Add or update a file: `{"action":"write_file","name":"example","base_version":"0.1.0","path":"references/notes.md","content":"reference content"}`

Use the current version from `view` as `base_version`. Read a related skill before updating it.
SKILL.md starts with YAML metadata and includes Markdown instructions. The lowercase kebab-case
`name` must match the command name and `description` describes when to use it. The store assigns
versions. If the workflow is environment-specific, list `cloud-browser`, `macos-desktop`, or
`windows-desktop` as appropriate under `environments`; otherwise omit that field.
"""

_HARNESS = r"""(?:harness|'harness'|"harness")"""
_SKILL = r"""(?:skill|'skill'|"skill")"""
_INVOCATION = re.compile(rf"\A{_HARNESS}[ \t]+{_SKILL}(?=$|[^\w./-])")
_WORD = r"""(?:[A-Za-z0-9_.,:/%+=@-]|'[^']*'|"(?:[^"\\]|\\[^\r\n])*"|\\[^\r\n])+"""
_LITERAL_COMMAND = re.compile(rf"{_WORD}(?:[ \t]+{_WORD})*")
_HEREDOC = re.compile(rf"""{_HARNESS}[ \t]+{_SKILL}[ \t]+manage[ \t]+<<[ \t]*'([A-Za-z_][A-Za-z0-9_]*)'[ \t]*""")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_MAX_COMMAND_BYTES = 256 * 1024
_WRITE_FIELDS = {
    "create": {"name", "content"},
    "edit": {"name", "base_version", "content"},
    "patch": {"name", "base_version", "old_string", "new_string"},
    "write_file": {"name", "base_version", "path", "content"},
}
_STANDALONE = "Use one standalone harness skill command with literal arguments."


def is_skill_command(command: str) -> bool:
    return bool(_INVOCATION.match(command.lstrip(" \t\r\n")))


def _arguments(command: str) -> list[str]:
    if len(command.encode("utf-8")) > _MAX_COMMAND_BYTES:
        raise ValueError("Skill command exceeds its size limit.")
    if _CONTROL.search(command):
        raise ValueError("Skill command contains a control character.")
    header, _, body = command.partition("\n")
    if match := _HEREDOC.fullmatch(header.rstrip("\r")):
        lines = body.split("\n")
        marker = match.group(1)
        end = next((index for index, line in enumerate(lines) if line.removesuffix("\r") == marker), None)
        if end is None or any(line.strip() for line in lines[end + 1:]):
            raise ValueError("End the quoted Skill JSON heredoc before adding any other commands.")
        return ["manage", "\n".join(lines[:end])]
    if not _LITERAL_COMMAND.fullmatch(command):
        raise ValueError(_STANDALONE)
    words = shlex.split(command, comments=False, posix=True)
    if words[:2] != ["harness", "skill"]:
        raise ValueError(_STANDALONE)
    return words[2:]


def _unique_fields(pairs: list[tuple[str, object]]) -> dict:
    fields = {}
    for key, value in pairs:
        if key in fields:
            raise ValueError(f"Duplicate Skill manage field: {key}.")
        fields[key] = value
    return fields


def _manage(payload: str, skills: SkillStore, environment: str | None) -> tuple[dict, str, bool]:
    try:
        values = json.loads(payload, object_pairs_hook=_unique_fields)
    except (json.JSONDecodeError, RecursionError) as error:
        raise ValueError("Skill manage requires a valid JSON object.") from error
    if not isinstance(values, dict):
        raise ValueError("Skill manage requires a JSON object.")
    action = values.pop("action", None)
    if not isinstance(action, str) or action not in _WRITE_FIELDS:
        raise ValueError("Skill manage action must be create, edit, patch, or write_file.")
    fields = _WRITE_FIELDS[action]
    if values.keys() != fields or not all(isinstance(value, str) for value in values.values()):
        raise ValueError(f"Skill manage {action} requires exactly these string fields: {', '.join(sorted(fields))}.")
    if action in {"create", "edit"}:
        result = skills.save_active(
            values["content"], name=values["name"], base_version=values.get("base_version"),
            environment=environment, allow_update=action == "edit",
        )
    elif action == "patch":
        result = skills.patch_active(
            values["name"], values["base_version"], values["old_string"], values["new_string"],
            environment=environment,
        )
    else:
        result = skills.write_skill_file(
            values["name"], values["base_version"], values["path"], values["content"],
            environment=environment,
        )
    return result, action, result["version"] != values.get("base_version")


def run_skill_command(command: str, skills: SkillStore, environment: str | None) -> dict | None:
    """Return a Bash-style result, or None when this is an ordinary shell command."""
    if not is_skill_command(command):
        return None
    try:
        args = _arguments(command.strip(" \t\r\n"))
        action = None
        changed = False
        match args:
            case [] | ["help"] | ["--help"]:
                result = {"help": SKILL_COMMAND_GUIDANCE}
            case ["list"]:
                result = [
                    {key: skill[key] for key in ("name", "description", "version", "environments")}
                    for skill in skills.active_index(environment)
                ]
            case ["view", name]:
                result = skills.view_active(name, environment)
            case ["view", name, path]:
                result = skills.view_active(name, environment, path)
            case ["manage", payload]:
                result, action, changed = _manage(payload, skills, environment)
                result = {key: result[key] for key in ("name", "version", "state", "environments")}
            case _:
                raise ValueError("Use harness skill list, view NAME [PATH], or manage '<JSON object>'.")
        response = {"exit_code": 0, "stdout": json.dumps(result, ensure_ascii=False, indent=2), "stderr": ""}
        if action:
            response.update(skill_changed=changed, skill_action=action)
        return response
    except ValueError as error:
        return {"exit_code": 1, "stdout": "", "stderr": str(error)}
    except (OSError, sqlite3.Error):
        return {"exit_code": 1, "stdout": "", "stderr": "Skill store is unavailable. Try again."}
