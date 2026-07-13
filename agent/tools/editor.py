"""
str_replace_editor tool — file viewing and editing.

Based directly on Anthropic's published str_replace_editor spec from their
SWE-bench 49% blog post. This is the second of the two core tools.

Commands:
    view       — Show file contents (with line numbers) or directory tree
    create     — Create a new file (fails if file already exists)
    str_replace — Replace an exact string in a file (fails if not unique)
    insert     — Insert text after a specific line number
    undo_edit  — Revert the last edit to a file

Design notes:
- str_replace requires EXACTLY ONE match. If old_str appears multiple times,
  the LLM is told to add more context to make it unique.
- Paths must be absolute. The tool rejects relative paths.
- undo_edit stores the previous file state in memory (per-task).
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from observability.metrics import metrics
from observability.tracing import get_tracer
from sandbox.docker_workspace import DockerWorkspace

tracer = get_tracer(__name__)

EDITOR_TOOL_SCHEMA: dict[str, Any] = {
    "name": "str_replace_editor",
    "description": (
        "Custom editing tool for viewing, creating, and editing files.\n\n"
        "COMMANDS:\n"
        "  view       — Show file contents with line numbers, or list directory contents.\n"
        "  create     — Create a new file. Fails if the file already exists.\n"
        "  str_replace — Replace a unique string in a file. old_str must match EXACTLY "
        "one occurrence. If it matches multiple times, include more surrounding lines "
        "to make it unique. Whitespace must match exactly.\n"
        "  insert     — Insert new_str AFTER the specified insert_line number.\n"
        "  undo_edit  — Revert the last edit made to the file at path.\n\n"
        "RULES:\n"
        "* path must be an ABSOLUTE path (e.g. /repo/src/module.py).\n"
        "* For str_replace: old_str must be unique in the file.\n"
        "* For create: the file must not already exist.\n"
        "* view on a directory shows files up to 2 levels deep.\n"
        "* Output longer than 8000 characters is clipped — use view_range to see "
        'specific line ranges: {"command": "view", "path": "/repo/file.py", '
        '"view_range": [10, 50]}\n'
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "enum": ["view", "create", "str_replace", "insert", "undo_edit"],
                "description": "The command to run.",
            },
            "path": {
                "type": "string",
                "description": "Absolute path to the file or directory.",
            },
            "file_text": {
                "type": "string",
                "description": "Required for 'create'. The content of the new file.",
            },
            "old_str": {
                "type": "string",
                "description": "Required for 'str_replace'. The exact string to replace. Must be unique in the file.",
            },
            "new_str": {
                "type": "string",
                "description": "Required for 'str_replace' and 'insert'. The replacement or inserted text.",
            },
            "insert_line": {
                "type": "integer",
                "description": "Required for 'insert'. new_str is inserted AFTER this line number.",
            },
            "view_range": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "Optional for 'view' on a file. [start_line, end_line]. Use -1 for end_line to go to EOF.",
            },
        },
        "required": ["command", "path"],
    },
}

# In-memory undo buffer: path → previous content
_undo_buffer: dict[str, str] = {}


def run_editor(workspace: DockerWorkspace, params: dict[str, Any]) -> str:
    """
    Execute an editor command and return a result string for the LLM.

    Args:
        workspace: The active DockerWorkspace.
        params:    The tool input dict from the LLM.

    Returns:
        A string the LLM reads as the tool result.
    """
    t0 = time.monotonic()
    command = params.get("command")
    path = params.get("path", "")

    with tracer.start_as_current_span("tool.editor") as span:
        span.set_attribute("command", command or "")
        span.set_attribute("path", path)

        try:
            result = _dispatch(workspace, command, path, params)
            error = False
        except Exception as exc:
            result = f"Error: {exc}"
            error = True

        duration_ms = int((time.monotonic() - t0) * 1000)
        metrics.tool_called("str_replace_editor", duration_ms=duration_ms, error=error)
        span.set_attribute("duration_ms", duration_ms)

    return result


def _dispatch(
    workspace: DockerWorkspace,
    command: str | None,
    path: str,
    params: dict[str, Any],
) -> str:
    if not path.startswith("/"):
        raise ValueError(
            f"Path must be absolute (start with /). Got: {path!r}. Use /repo/path/to/file.py"
        )

    if command == "view":
        return _view(workspace, path, params.get("view_range"))
    elif command == "create":
        return _create(workspace, path, params.get("file_text", ""))
    elif command == "str_replace":
        return _str_replace(workspace, path, params.get("old_str", ""), params.get("new_str", ""))
    elif command == "insert":
        return _insert(workspace, path, params.get("insert_line", 0), params.get("new_str", ""))
    elif command == "undo_edit":
        return _undo(workspace, path)
    else:
        raise ValueError(
            f"Unknown command: {command!r}. Must be one of: view, create, str_replace, insert, undo_edit"
        )


def _view(
    workspace: DockerWorkspace,
    path: str,
    view_range: list[int] | None,
) -> str:
    """Show file with line numbers, or directory tree."""
    # Check if path is a directory
    is_dir_result = workspace.run(f"test -d '{path}' && echo dir || echo file")
    is_dir = is_dir_result.stdout.strip() == "dir"

    if is_dir:
        result = workspace.run(
            f"find '{path}' -not -path '*/.git/*' -not -name '*.pyc' | sort | head -200"
        )
        return f"Directory listing for {path}:\n{result.stdout}"

    # File view
    if view_range and len(view_range) == 2:
        start, end = view_range
        if end == -1:
            result = workspace.run(
                f"awk 'NR>={start}' '{path}' | nl -ba -nrz -v{start} | head -200"
            )
        else:
            result = workspace.run(
                f"sed -n '{start},{end}p' '{path}' | cat -n | awk '{{printf \"%d\\t%s\\n\", NR+{start - 1}, $0}}'"
            )
            result = workspace.run(
                f"awk 'NR>={start} && NR<={end}' '{path}' | nl -ba -nrz -v{start}"
            )
    else:
        result = workspace.run(f"cat -n '{path}'")

    if not result.success:
        raise FileNotFoundError(f"Cannot view {path}: {result.stderr}")

    output = result.stdout
    MAX = 8000
    if len(output) > MAX:
        output = (
            output[:MAX] + f"\n<clipped: showing first {MAX} chars — use view_range to see more>"
        )

    return f"File: {path}\n{output}"


def _create(workspace: DockerWorkspace, path: str, file_text: str) -> str:
    """Create a new file. Fails if it already exists."""
    if workspace.file_exists(path):
        raise FileExistsError(
            f"File already exists: {path}. Use str_replace to edit it, or choose a different path."
        )

    # Ensure parent directory exists
    parent = str(Path(path).parent)
    workspace.run(f"mkdir -p '{parent}'")
    workspace.write_file(path, file_text)
    lines = len(file_text.splitlines())
    return f"Created {path} ({lines} lines)"


def _str_replace(workspace: DockerWorkspace, path: str, old_str: str, new_str: str) -> str:
    """
    Replace old_str with new_str in the file.
    Requires exactly one match — errors if zero or multiple matches found.
    """
    try:
        content = workspace.read_file(path)
    except FileNotFoundError:
        raise FileNotFoundError(f"File not found: {path}")

    count = content.count(old_str)
    if count == 0:
        raise ValueError(
            f"old_str not found in {path}.\n"
            "Check for exact whitespace/indentation match. "
            "Use 'view' to confirm the exact text in the file."
        )
    if count > 1:
        raise ValueError(
            f"Found {count} matches for old_str in {path}. "
            "Add more surrounding lines to old_str to make it unique."
        )

    # Save to undo buffer
    _undo_buffer[path] = content

    new_content = content.replace(old_str, new_str, 1)
    workspace.write_file(path, new_content)

    old_lines = old_str.count("\n") + 1
    new_lines = new_str.count("\n") + 1
    return (
        f"Replaced in {path}: {old_lines} line(s) → {new_lines} line(s).\n"
        f"Use undo_edit to revert if needed."
    )


def _insert(workspace: DockerWorkspace, path: str, insert_line: int, new_str: str) -> str:
    """Insert new_str after insert_line in the file."""
    try:
        content = workspace.read_file(path)
    except FileNotFoundError:
        raise FileNotFoundError(f"File not found: {path}")

    lines = content.splitlines(keepends=True)
    if insert_line > len(lines):
        raise ValueError(
            f"insert_line {insert_line} is beyond the end of the file ({len(lines)} lines)."
        )

    _undo_buffer[path] = content

    new_lines = new_str if new_str.endswith("\n") else new_str + "\n"
    lines.insert(insert_line, new_lines)
    workspace.write_file(path, "".join(lines))

    return f"Inserted {new_str.count(chr(10)) + 1} line(s) after line {insert_line} in {path}."


def _undo(workspace: DockerWorkspace, path: str) -> str:
    """Revert the last edit to path."""
    if path not in _undo_buffer:
        raise ValueError(f"No undo history for {path}.")

    previous = _undo_buffer.pop(path)
    workspace.write_file(path, previous)
    return f"Reverted {path} to previous state."
