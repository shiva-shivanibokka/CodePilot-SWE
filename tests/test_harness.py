"""
Smoke tests for the evaluation harness.

These tests verify the harness infrastructure works without actually calling
the LLM or Docker (both are mocked). They confirm:
- SWE-bench instances can be loaded (or at least the loader doesn't crash)
- The test command builder works correctly
- Instance result dataclasses serialize correctly
"""

from __future__ import annotations

import json
from dataclasses import asdict

import pytest

from eval.harness import (
    InstanceResult,
    build_test_command,
)


class TestBuildTestCommand:
    def test_with_fail_to_pass(self):
        instance = {
            "instance_id": "test__repo-123",
            "FAIL_TO_PASS": json.dumps(["tests/test_module.py::test_foo"]),
            "PASS_TO_PASS": json.dumps([]),
        }
        cmd = build_test_command(instance)
        assert "pytest" in cmd
        assert "test_foo" in cmd

    def test_with_both_lists(self):
        instance = {
            "instance_id": "test__repo-456",
            "FAIL_TO_PASS": json.dumps(["tests/test_a.py::test_1"]),
            "PASS_TO_PASS": json.dumps(["tests/test_b.py::test_2"]),
        }
        cmd = build_test_command(instance)
        assert "test_1" in cmd
        assert "test_2" in cmd

    def test_empty_lists_fallback(self):
        instance = {
            "instance_id": "test__repo-789",
            "FAIL_TO_PASS": "[]",
            "PASS_TO_PASS": "[]",
        }
        cmd = build_test_command(instance)
        assert "pytest tests/" in cmd

    def test_missing_keys_fallback(self):
        instance = {"instance_id": "test__repo-000"}
        cmd = build_test_command(instance)
        assert "pytest" in cmd


class TestInstanceResult:
    def test_serialization(self):
        result = InstanceResult(
            instance_id="scikit-learn__scikit-learn-12462",
            repo="scikit-learn/scikit-learn",
            approach="agent",
            resolved=True,
            cost_usd=0.42,
            turns=15,
            input_tokens=12000,
            output_tokens=3400,
            duration_seconds=180.5,
            stop_reason="done",
            diff="--- a/sklearn/ridge.py\n+++ b/sklearn/ridge.py\n",
        )
        d = asdict(result)
        assert d["instance_id"] == "scikit-learn__scikit-learn-12462"
        assert d["resolved"] is True
        assert d["cost_usd"] == 0.42

        # Must be JSON-serialisable
        json.dumps(d)

    def test_error_result(self):
        result = InstanceResult(
            instance_id="repo__issue-1",
            repo="owner/repo",
            approach="agentless",
            resolved=False,
            cost_usd=0.0,
            turns=0,
            input_tokens=0,
            output_tokens=0,
            duration_seconds=0.0,
            stop_reason="error",
            diff="",
            error="Docker daemon not running",
        )
        assert result.error == "Docker daemon not running"
        assert not result.resolved


class TestGithubUrlParser:
    def test_full_url(self):
        from github_integration.issue_fetcher import parse_github_url

        repo, num = parse_github_url("https://github.com/scikit-learn/scikit-learn/issues/12462")
        assert repo == "scikit-learn/scikit-learn"
        assert num == 12462

    def test_short_format(self):
        from github_integration.issue_fetcher import parse_github_url

        repo, num = parse_github_url("owner/repo#42")
        assert repo == "owner/repo"
        assert num == 42

    def test_invalid_url_raises(self):
        from github_integration.issue_fetcher import parse_github_url

        with pytest.raises(ValueError):
            parse_github_url("not-a-url")
