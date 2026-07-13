"""
Bash tool — the agent's primary way of interacting with the codebase.

This is the most important tool. Anthropic's 49% SWE-bench result used a
persistent bash session as its only execution tool. The key design insight:
the shell is STATEFUL across calls. cd, export, and source all persist.

Design:
- The sandbox container runs a persistent bash process.
- We send commands via docker exec (each exec is independent at the OS level)
  but we emulate statefulness by tracking the current working directory and
  running each command inside it.
- Commands are truncated at MAX_OUTPUT_CHARS to prevent context blowup.
- Exit codes are always reported so the LLM can self-correct.

Tool schema (returned to the LLM as a tool definition):
    name: "bash"
    description: <carefully engineered — this is the ACI>
    input_schema: {command: string}
"""

from __future__ import annotations

import time
from typing import Any

from observability.metrics import metrics
from observability.tracing import get_tracer
from sandbox.docker_workspace import CommandResult, DockerWorkspace

tracer = get_tracer(__name__)

# Truncate tool output to this many characters to avoid filling the context window.
# The LLM will see a truncation notice and can ask for specific lines.
MAX_OUTPUT_CHARS = 8_000

# The tool schema sent to the Anthropic API.
# The description IS the engineering — it prevents the most common LLM mistakes.
BASH_TOOL_SCHEMA: dict[str, Any] = {
    "name": "bash",
    "description": (
        "Run commands in a bash shell inside a sandboxed repository environment.\n\n"
        "IMPORTANT RULES:\n"
        "* State IS persistent across calls: cd, export, source, and variable "
        "assignments all persist between bash calls in the same session.\n"
        "* You do NOT have internet access. Do not attempt curl/wget to external URLs.\n"
        "* Always use ABSOLUTE paths (e.g. /repo/src/module.py) to avoid path errors "
        "after cd commands.\n"
        "* Output is truncated at 8000 characters. For long files use: "
        "sed -n '10,50p' /path/to/file  to view specific line ranges.\n"
        "* To run tests: pytest tests/ -x -q --tb=short\n"
        "* To view file with line numbers: cat -n /repo/path/to/file.py\n"
        "* Background long-running commands with &: python server.py &\n"
        "* The exit code is always shown. Non-zero exit = command failed.\n"
        "* Do not produce commands with very large outputs (e.g. cat on huge files).\n"
        "* The repository is at /repo. Always work relative to /repo.\n"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The bash command to run. Must be a single shell command or pipeline.",
            }
        },
        "required": ["command"],
    },
}


def run_bash(workspace: DockerWorkspace, command: str) -> str:
    """
    Execute a bash command in the sandbox and return a formatted result string
    suitable for feeding back to the LLM as a tool result.

    Args:
        workspace: The active DockerWorkspace for this task.
        command:   The shell command to run.

    Returns:
        A string the LLM reads as the tool result. Format:
            <exit_code>0</exit_code>
            <output>
            ... command output ...
            </output>
        or on error:
            <exit_code>1</exit_code>
            <output>
            ... error message ...
            </output>
    """
    t0 = time.monotonic()

    with tracer.start_as_current_span("tool.bash") as span:
        span.set_attribute("command", command[:200])

        result: CommandResult = workspace.run(command)
        duration_ms = int((time.monotonic() - t0) * 1000)

        metrics.tool_called(
            tool_name="bash",
            duration_ms=duration_ms,
            error=not result.success,
        )

        span.set_attribute("exit_code", result.exit_code)
        span.set_attribute("duration_ms", duration_ms)
        span.set_attribute("timed_out", result.timed_out)

    # Format the output
    output = result.output

    if result.timed_out:
        output = f"[COMMAND TIMED OUT after {workspace._timeout_seconds}s]\n{output}"

    # Truncate if needed
    truncated = False
    if len(output) > MAX_OUTPUT_CHARS:
        output = output[:MAX_OUTPUT_CHARS]
        truncated = True

    lines = [
        f"<exit_code>{result.exit_code}</exit_code>",
        f"<duration_ms>{duration_ms}</duration_ms>",
        "<output>",
        output,
        "</output>",
    ]

    if truncated:
        lines.append(
            f"<truncated>Output was truncated at {MAX_OUTPUT_CHARS} characters. "
            "Use 'sed -n START,ENDp /path/to/file' to view specific sections.</truncated>"
        )

    return "\n".join(lines)
