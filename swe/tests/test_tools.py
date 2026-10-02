"""
Unit tests for the agent tools (bash, editor, search).

These tests use a mock DockerWorkspace so they don't require Docker.
They verify the tool logic: output formatting, error handling,
str_replace uniqueness checks, undo, etc.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from sandbox.docker_workspace import CommandResult


def make_workspace(
    files: dict[str, str] | None = None,
    run_responses: dict[str, CommandResult] | None = None,
) -> MagicMock:
    """Create a mock DockerWorkspace."""
    ws = MagicMock()
    ws.task_id = "test-1234"
    _files = dict(files or {})

    def mock_read_file(path):
        if path in _files:
            return _files[path]
        raise FileNotFoundError(f"Not found: {path}")

    def mock_write_file(path, content):
        _files[path] = content

    def mock_file_exists(path):
        return path in _files

    def mock_run(cmd, timeout=120, workdir="/repo"):
        if run_responses and cmd in run_responses:
            return run_responses[cmd]
        # Default: successful empty result
        return CommandResult(command=cmd, stdout="", stderr="", exit_code=0)

    ws.read_file.side_effect = mock_read_file
    ws.write_file.side_effect = mock_write_file
    ws.file_exists.side_effect = mock_file_exists
    ws.run.side_effect = mock_run
    ws._files = _files
    return ws


# ── Bash tool tests ────────────────────────────────────────────────────────────


class TestBashTool:
    def test_success_output_formatted(self):
        from agent.tools.bash import run_bash

        ws = make_workspace(
            run_responses={
                "ls /repo": CommandResult(
                    command="ls /repo",
                    stdout="README.md\nsrc/\ntests/",
                    stderr="",
                    exit_code=0,
                )
            }
        )
        result = run_bash(ws, "ls /repo")
        assert "<exit_code>0</exit_code>" in result
        assert "README.md" in result

    def test_nonzero_exit_included(self):
        from agent.tools.bash import run_bash

        ws = make_workspace(
            run_responses={
                "python broken.py": CommandResult(
                    command="python broken.py",
                    stdout="",
                    stderr="SyntaxError: invalid syntax",
                    exit_code=1,
                )
            }
        )
        result = run_bash(ws, "python broken.py")
        assert "<exit_code>1</exit_code>" in result
        assert "SyntaxError" in result

    def test_output_truncated_at_limit(self):
        from agent.tools.bash import MAX_OUTPUT_CHARS, run_bash

        ws = make_workspace(
            run_responses={
                "cat big_file": CommandResult(
                    command="cat big_file",
                    stdout="x" * (MAX_OUTPUT_CHARS + 1000),
                    stderr="",
                    exit_code=0,
                )
            }
        )
        result = run_bash(ws, "cat big_file")
        assert "<truncated>" in result
        assert len(result) < MAX_OUTPUT_CHARS + 500  # output is truncated

    def test_duration_included(self):
        from agent.tools.bash import run_bash

        ws = make_workspace(
            run_responses={
                "echo hi": CommandResult(command="echo hi", stdout="hi", stderr="", exit_code=0)
            }
        )
        result = run_bash(ws, "echo hi")
        assert "<duration_ms>" in result


# ── Editor tool tests ──────────────────────────────────────────────────────────


class TestEditorTool:
    def test_view_file(self):
        from agent.tools.editor import run_editor

        ws = make_workspace(
            run_responses={
                "test -d '/repo/module.py' && echo dir || echo file": CommandResult(
                    command="", stdout="file\n", stderr="", exit_code=0
                ),
                "cat -n '/repo/module.py'": CommandResult(
                    command="",
                    stdout="     1\tdef hello():\n     2\t    pass\n",
                    stderr="",
                    exit_code=0,
                ),
            }
        )
        result = run_editor(ws, {"command": "view", "path": "/repo/module.py"})
        assert "hello" in result or "module.py" in result

    def test_str_replace_success(self):
        from agent.tools.editor import run_editor

        content = "def hello():\n    return 'hi'\n"
        ws = make_workspace(files={"/repo/module.py": content})
        result = run_editor(
            ws,
            {
                "command": "str_replace",
                "path": "/repo/module.py",
                "old_str": "return 'hi'",
                "new_str": "return 'hello'",
            },
        )
        assert "Replaced" in result
        assert ws._files["/repo/module.py"] == "def hello():\n    return 'hello'\n"

    def test_str_replace_not_found_raises(self):
        from agent.tools.editor import run_editor

        ws = make_workspace(files={"/repo/module.py": "def foo(): pass\n"})
        result = run_editor(
            ws,
            {
                "command": "str_replace",
                "path": "/repo/module.py",
                "old_str": "def bar(): pass",
                "new_str": "def bar(): return 1",
            },
        )
        assert "not found" in result.lower()

    def test_str_replace_multiple_matches_raises(self):
        from agent.tools.editor import run_editor

        content = "pass\npass\npass\n"
        ws = make_workspace(files={"/repo/module.py": content})
        result = run_editor(
            ws,
            {
                "command": "str_replace",
                "path": "/repo/module.py",
                "old_str": "pass",
                "new_str": "return None",
            },
        )
        assert "3 matches" in result or "multiple" in result.lower()

    def test_create_new_file(self):
        from agent.tools.editor import run_editor

        ws = make_workspace()
        ws.run.side_effect = lambda cmd, **kw: CommandResult(
            command=cmd, stdout="", stderr="", exit_code=0
        )
        result = run_editor(
            ws,
            {
                "command": "create",
                "path": "/repo/new_file.py",
                "file_text": "print('hello')\n",
            },
        )
        assert "Created" in result

    def test_create_existing_file_raises(self):
        from agent.tools.editor import run_editor

        ws = make_workspace(files={"/repo/existing.py": "# exists\n"})
        result = run_editor(
            ws,
            {
                "command": "create",
                "path": "/repo/existing.py",
                "file_text": "new content\n",
            },
        )
        assert "already exists" in result.lower()

    def test_undo_edit(self):
        from agent.tools.editor import run_editor

        content = "original\n"
        ws = make_workspace(files={"/repo/module.py": content})

        # First do an edit
        run_editor(
            ws,
            {
                "command": "str_replace",
                "path": "/repo/module.py",
                "old_str": "original",
                "new_str": "changed",
            },
        )
        assert ws._files["/repo/module.py"] == "changed\n"

        # Then undo
        result = run_editor(ws, {"command": "undo_edit", "path": "/repo/module.py"})
        assert "Reverted" in result
        assert ws._files["/repo/module.py"] == "original\n"

    def test_relative_path_rejected(self):
        from agent.tools.editor import run_editor

        ws = make_workspace()
        result = run_editor(ws, {"command": "view", "path": "relative/path.py"})
        assert "absolute" in result.lower() or "Error" in result


# ── Context budget manager tests ───────────────────────────────────────────────


class TestContextManager:
    def test_count_tokens_returns_int(self):
        from agent.context import count_tokens

        n = count_tokens("Hello, world! This is a test.")
        assert isinstance(n, int)
        assert n > 0

    def test_should_compress_false_when_small(self):
        from agent.context import should_compress

        messages = [{"role": "user", "content": "Hello"}]
        assert not should_compress(messages)

    def test_compress_reduces_length(self):
        from agent.context import MIN_MESSAGES_TO_KEEP, compress_messages

        # Build a message list longer than the minimum
        messages = []
        for i in range(20):
            messages.append(
                {
                    "role": "assistant",
                    "content": [{"type": "text", "text": f"Thinking step {i}" * 100}],
                }
            )
            messages.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": f"id{i}",
                            "content": f"Result {i}" * 50,
                        }
                    ],
                }
            )

        compressed = compress_messages(messages)
        # Compressed should be shorter than original
        assert len(compressed) < len(messages)
        # Should keep at least the tail
        assert len(compressed) >= MIN_MESSAGES_TO_KEEP

    def test_get_budget_status_returns_dict(self):
        from agent.context import get_budget_status

        messages = [{"role": "user", "content": "test"}]
        status = get_budget_status(messages)
        assert "used_tokens" in status
        assert "budget_tokens" in status
        assert "percent_used" in status
        assert 0 <= status["percent_used"] <= 100
