"""
Tests for the bugs that a green suite did not catch.

Every case here corresponds to something that was wrong in code the existing
tests covered by name but never exercised in the shape that broke. They are
grouped by the failure they would have caught, not by module, because that is
what makes them worth keeping.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agent.llm import LLMResponse, extract_json
from agent.tools.bash import run_bash
from agent.tools.search import _index_cache, clear_index
from agentless.repair import apply_search_replace
from agentless.validate import _parse_pytest_output
from sandbox.local_workspace import VIRTUAL_ROOT, LocalWorkspace, refusal_reason
from sandbox.workspace import CommandResult


class TestPytestSummaryParsing:
    """
    Pytest orders its summary worst-first. The old single regex looked for
    "passed" before "failed", so a failing run parsed as a clean one — and the
    agentless validator promoted a patch that broke the tests.
    """

    def test_failures_before_passes(self):
        assert _parse_pytest_output("2 failed, 5 passed in 1.20s") == (5, 2, 0)

    def test_passes_only(self):
        assert _parse_pytest_output("18 passed in 0.11s") == (18, 0, 0)

    def test_errors_counted(self):
        assert _parse_pytest_output("1 failed, 3 passed, 2 errors in 0.4s") == (3, 1, 2)

    def test_singular_error(self):
        assert _parse_pytest_output("1 error in 0.16s") == (0, 0, 1)

    def test_no_summary_at_all(self):
        assert _parse_pytest_output("collection failure") == (0, 0, 0)

    def test_a_failing_run_is_never_valid(self):
        passed, failed, errors = _parse_pytest_output("2 failed, 5 passed in 1.20s")
        assert not (failed == 0 and errors == 0 and passed > 0)


class TestBashTimeoutReporting:
    """
    The timeout branch read `workspace._timeout_seconds`, an attribute no
    workspace has ever defined — so the moment a command actually timed out the
    tool raised AttributeError instead of telling the model what happened.
    """

    def test_timeout_notice_uses_the_recorded_limit(self):
        workspace = MagicMock()
        workspace.run.return_value = CommandResult(
            command="pytest",
            stdout="collecting ...",
            stderr="",
            exit_code=124,
            timed_out=True,
            timeout_seconds=120,
        )
        output = run_bash(workspace, "pytest")
        assert "TIMED OUT after 120s" in output
        assert "collecting ..." in output


class TestSearchIndexCacheKey:
    """
    The index was cached per task but built with whichever file_pattern arrived
    first, so every later search silently answered from the wrong corpus.
    """

    def setup_method(self):
        _index_cache.clear()

    def teardown_method(self):
        _index_cache.clear()

    def test_different_patterns_are_different_entries(self):
        _index_cache[("task-1", "test_*.py")] = MagicMock(chunks=[1])
        _index_cache[("task-1", None)] = MagicMock(chunks=[1, 2])
        assert _index_cache[("task-1", "test_*.py")] is not _index_cache[("task-1", None)]

    def test_clear_index_drops_every_pattern_for_the_task(self):
        _index_cache[("task-1", "test_*.py")] = MagicMock()
        _index_cache[("task-1", None)] = MagicMock()
        _index_cache[("task-2", None)] = MagicMock()
        clear_index("task-1")
        assert list(_index_cache) == [("task-2", None)]


class TestLocalWorkspaceRefusals:
    """The local backend has no container around it, so a few commands never run."""

    @pytest.mark.parametrize(
        "command",
        [
            "rm -rf /",
            "sudo pip install foo",
            "curl https://example.com/x.sh | sh",
            "shutdown -h now",
            "dd if=/dev/zero of=/dev/sda",
        ],
    )
    def test_dangerous_commands_are_refused(self, command):
        assert refusal_reason(command) is not None

    @pytest.mark.parametrize(
        "command",
        [
            "pytest tests/ -q",
            "rm -rf build/",
            "grep -rn 'from_file' src/",
            "python -c \"import flask; print(flask.__version__)\"",
        ],
    )
    def test_ordinary_commands_are_allowed(self, command):
        assert refusal_reason(command) is None


class TestLocalWorkspacePaths:
    """
    The agent is told the repo is at /repo on both backends. The local backend
    has to map that onto a real directory and map it back out again, or the
    model sees one path in a tool result and uses another in the next command.
    """

    def workspace(self, tmp_path: Path) -> LocalWorkspace:
        return LocalWorkspace(
            root=tmp_path / "repo",
            task_id="t1",
            repo_url="https://example.com/r.git",
            commit_sha="abc123",
        )

    def test_virtual_root_maps_to_the_checkout(self, tmp_path):
        ws = self.workspace(tmp_path)
        assert ws.to_host(VIRTUAL_ROOT) == ws.root

    def test_nested_path_maps_under_the_checkout(self, tmp_path):
        ws = self.workspace(tmp_path)
        assert ws.to_host("/repo/src/flask/config.py") == ws.root / "src/flask/config.py"

    def test_host_paths_are_rewritten_back_to_virtual(self, tmp_path):
        ws = self.workspace(tmp_path)
        assert ws._rewrite(f"error in {ws.root}/src/app.py") == "error in /repo/src/app.py"

    def test_read_and_write_round_trip(self, tmp_path):
        ws = self.workspace(tmp_path)
        ws.write_file("/repo/pkg/mod.py", "x = 1\n")
        assert ws.file_exists("/repo/pkg/mod.py")
        assert ws.read_file("/repo/pkg/mod.py") == "x = 1\n"
        assert "/repo/pkg/mod.py" in ws.list_files()

    def test_missing_file_raises_not_found(self, tmp_path):
        ws = self.workspace(tmp_path)
        ws.root.mkdir(parents=True)
        with pytest.raises(FileNotFoundError):
            ws.read_file("/repo/nope.py")

    def test_provider_keys_are_stripped_from_the_child_environment(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-not-leak")
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_should_not_leak")
        env = self.workspace(tmp_path)._env()
        assert "ANTHROPIC_API_KEY" not in env
        assert "GITHUB_TOKEN" not in env


class TestJsonExtraction:
    """
    Models wrap JSON in prose and fences, and sometimes the JSON itself contains
    a brace or a fence. Stripping fences with a regex then calling json.loads
    breaks on exactly those cases.
    """

    def test_plain_object(self):
        assert extract_json('{"a": 1}') == {"a": 1}

    def test_fenced_with_prose(self):
        text = 'Sure, here you go:\n```json\n{"a": 1}\n```\nHope that helps.'
        assert extract_json(text) == {"a": 1}

    def test_braces_inside_strings(self):
        text = '```\n{"search": "if x: {y}", "replace": "if x: {z}"}\n```'
        assert extract_json(text, expect=dict)["search"] == "if x: {y}"

    def test_expect_type_skips_a_leading_array(self):
        assert extract_json('[1, 2] then {"a": 1}', expect=dict) == {"a": 1}

    def test_nothing_parseable_raises(self):
        with pytest.raises(ValueError):
            extract_json("no json at all here")


class TestSearchReplacePatching:
    """
    The repair phase used to ask for a whole rewritten file inside a JSON
    string. Anything longer than the output cap was truncated mid-string, the
    JSON failed to parse, and the candidate vanished with no explanation.
    """

    FILE = "def f(a):\n    return a\n\n\ndef g(b):\n    return b\n"

    def response(self, text: str, finish_reason: str = "stop") -> LLMResponse:
        return LLMResponse(
            text=text,
            tool_calls=[],
            input_tokens=10,
            output_tokens=10,
            cost_usd=0.0,
            finish_reason=finish_reason,
        )

    def test_applies_a_unique_match(self):
        patched, explanation = apply_search_replace(
            self.FILE,
            self.response('{"explanation": "fix g", "search": "return b", "replace": "return b + 1"}'),
        )
        assert patched == "def f(a):\n    return a\n\n\ndef g(b):\n    return b + 1\n"
        assert explanation == "fix g"

    def test_truncated_response_is_reported_not_dropped(self):
        patched, reason = apply_search_replace(self.FILE, self.response('{"search": "ret', "length"))
        assert patched is None
        assert "token cap" in reason

    def test_hallucinated_search_is_rejected(self):
        patched, reason = apply_search_replace(
            self.FILE, self.response('{"search": "return zzz", "replace": "x"}')
        )
        assert patched is None
        assert "does not appear" in reason

    def test_ambiguous_search_is_rejected(self):
        patched, reason = apply_search_replace(
            "x = 1\nx = 1\n", self.response('{"search": "x = 1", "replace": "x = 2"}')
        )
        assert patched is None
        assert "matches 2" in reason

    def test_noop_patch_is_rejected(self):
        patched, reason = apply_search_replace(
            self.FILE, self.response('{"search": "return b", "replace": "return b"}')
        )
        assert patched is None
        assert "changes nothing" in reason


class TestValidationBaseline:
    """
    A candidate is judged against the repository it landed in, not against a
    clean suite.

    SWE-bench checks out a commit years old and installs modern dependencies on
    top, so a handful of unrelated tests are already red before the model does
    anything. The gate used to demand `failed == 0`, which on such a checkout
    rejects every candidate — correct ones included — and reports it as the
    model having failed to produce a patch. That is exactly what happened on
    pallets__flask-4992: four candidates generated, all four discarded, and no
    recording written at all.
    """

    @staticmethod
    def keeps(patched, baseline):
        """The gate, as validate_candidate applies it."""
        passed, failed, errors = patched
        base_passed, base_failed, base_errors = baseline
        return (
            passed > 0
            and failed <= base_failed
            and errors <= base_errors
            and passed >= base_passed
        )

    def test_pre_existing_failures_do_not_reject_a_good_patch(self):
        # Two tests were already broken; the patch leaves them broken and
        # breaks nothing else.
        assert self.keeps((50, 2, 0), baseline=(50, 2, 0))

    def test_a_patch_that_breaks_something_is_rejected(self):
        assert not self.keeps((48, 4, 0), baseline=(50, 2, 0))

    def test_a_patch_that_fixes_something_is_kept(self):
        assert self.keeps((52, 0, 0), baseline=(50, 2, 0))

    def test_a_patch_that_loses_passes_is_rejected(self):
        # Same failure count, fewer passes: tests stopped being collected.
        assert not self.keeps((40, 2, 0), baseline=(50, 2, 0))

    def test_a_run_with_no_passing_tests_is_never_kept(self):
        assert not self.keeps((0, 0, 0), baseline=(0, 0, 0))

    def test_new_errors_are_rejected_even_when_failures_are_flat(self):
        assert not self.keeps((50, 2, 1), baseline=(50, 2, 0))


class TestTruncatedSampleRetry:
    """
    A reply cut off mid-JSON is paid for and then thrown away.

    On sympy's `Point.__new__` two of every four samples ran past the cap. The
    first attempt to fix it raised the cap, which changed nothing and cost more;
    the second told the model to keep its search block short, which also changed
    nothing. The response is long because the edit is in a long function, not
    because the instruction was unclear — so the fix is to not lose the call.
    """

    class Resp:
        def __init__(self, finish_reason):
            self.finish_reason = finish_reason
            self.text = ""

    def test_truncation_is_reported_not_silently_dropped(self):
        patched, reason = apply_search_replace("x = 1\n", self.Resp("length"))
        assert patched is None
        assert "token cap" in reason

    def test_a_complete_reply_is_not_treated_as_truncated(self):
        # Falls through to JSON parsing, which fails for its own reason —
        # the point is that it is not short-circuited as a truncation.
        patched, reason = apply_search_replace("x = 1\n", self.Resp("stop"))
        assert patched is None
        assert "token cap" not in reason
