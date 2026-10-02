"""Grading: a clean tree, the agent's source diff only, every required id exactly.

The positive control (the gold patch resolves) comes first, so that every
"does not resolve" below is a statement about the patch and not about a
harness that resolves nothing.
"""

from __future__ import annotations

import json

import pytest

from codepilot.bench import swebench, testlog
from codepilot.bench.environment import BenchEnv
from codepilot.bench.grading import filter_source_diff
from tests.benchfixture import GOLD_PATCH, make_task

FORCE_PASS_CONFTEST = '''\
import pytest


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    report.outcome = "passed"
'''


def _new_file_diff(path: str, content: str) -> str:
    lines = content.splitlines()
    body = "".join(f"+{line}\n" for line in lines)
    return (
        f"diff --git a/{path} b/{path}\nnew file mode 100644\n--- /dev/null\n"
        f"+++ b/{path}\n@@ -0,0 +1,{len(lines)} @@\n{body}"
    )


@pytest.fixture
async def env_and_task(tmp_path):
    instance, _ = make_task(tmp_path)
    env = await BenchEnv.create(
        instance["repo_url"], instance["base_commit"], backend="local",
        install=False, venv=False, task_id="calc",
    )
    yield env, instance
    await env.close()


async def test_the_gold_patch_resolves(env_and_task):
    env, instance = env_and_task
    report = await swebench.grade(env, instance, GOLD_PATCH)
    assert report.applied, report.detail
    assert report.resolved, (report.failures, report.log_tail)
    assert (report.f2p_passed, report.f2p_total) == (1, 1)
    assert (report.p2p_passed, report.p2p_total) == (1, 1)


async def test_no_patch_does_not_resolve(env_and_task):
    env, instance = env_and_task
    report = await swebench.grade(env, instance, "")
    assert not report.resolved


async def test_an_agent_written_conftest_cannot_flip_the_result(env_and_task):
    """The diff carries no fix, only a conftest.py that rewrites every test
    outcome to "passed". Run in the agent's own tree, the held-out test would
    pass; graded here, the conftest never reaches the run."""
    env, instance = env_and_task
    diff = _new_file_diff("tests/conftest.py", FORCE_PASS_CONFTEST)

    # Show the attack works when it is live: write it and run the test patch.
    (env.root / "tests" / "conftest.py").write_text(FORCE_PASS_CONFTEST, encoding="utf-8")
    assert env.apply(instance["test_patch"])[0]
    live = await env.run(swebench.build_test_spec(instance).command)
    assert testlog.lookup(testlog.parse_pytest(live.combined), "tests/test_calc.py::test_add") == "PASSED", (
        "the attack does not work, so this test would prove nothing"
    )

    report = await swebench.grade(env, instance, diff)
    assert not report.resolved
    assert "tests/conftest.py" in report.dropped


async def test_a_conftest_hidden_in_an_ignored_directory_is_removed(env_and_task):
    """Never in the diff (the directory is .gitignored), but pytest would still
    load it if it were on the path. Restoring the tree removes it."""
    env, instance = env_and_task
    (env.root / "build").mkdir()
    (env.root / "build" / "conftest.py").write_text(FORCE_PASS_CONFTEST, encoding="utf-8")
    instance = dict(instance)
    report = await swebench.grade(env, instance, "")
    assert not (env.root / "build" / "conftest.py").exists()
    assert not report.resolved


async def test_an_agent_that_deletes_the_failing_test_gains_nothing(env_and_task):
    env, instance = env_and_task
    diff = (
        "diff --git a/tests/test_calc.py b/tests/test_calc.py\n--- a/tests/test_calc.py\n"
        "+++ b/tests/test_calc.py\n@@ -1,5 +1,5 @@\n from calc import double\n \n \n"
        " def test_double():\n-    assert double(2) == 4\n+    assert True\n"
    )
    report = await swebench.grade(env, instance, diff)
    assert not report.resolved
    assert "tests/test_calc.py" in report.dropped


async def test_a_required_test_that_never_ran_is_a_failure_not_a_pass(env_and_task):
    """The -k reproduction: `-k "test_ad"` would select test_add and pass.
    Exact matching finds no test named test_ad, so the instance fails."""
    env, instance = env_and_task
    instance = dict(instance, FAIL_TO_PASS=json.dumps(["test_ad"]))
    report = await swebench.grade(env, instance, GOLD_PATCH)
    assert not report.resolved
    assert report.failures == {"test_ad": "missing"}


async def test_every_required_test_is_graded_no_cap(env_and_task):
    """The cap reproduction: with 25 PASS_TO_PASS ids, B graded the first 20.
    Here the 25th is a test that does not exist, and it is caught."""
    env, instance = env_and_task
    ids = ["tests/test_calc.py::test_double"] * 24 + ["tests/test_calc.py::test_nonexistent"]
    instance = dict(instance, PASS_TO_PASS=json.dumps(ids))
    report = await swebench.grade(env, instance, GOLD_PATCH)
    assert report.p2p_total == 25
    assert not report.resolved
    assert report.failures == {"tests/test_calc.py::test_nonexistent": "missing"}


def test_the_graded_command_has_no_exitfirst_no_k_and_no_cap():
    ids = [f"tests/test_x.py::test_{i}" for i in range(40)]
    spec = swebench.build_test_spec(
        {"FAIL_TO_PASS": json.dumps(ids[:1]), "PASS_TO_PASS": json.dumps(ids[1:]), "test_patch": ""}
    )
    assert " -x" not in spec.command and " -k " not in spec.command
    assert spec.targets == ["tests/test_x.py"]


def test_bare_names_run_the_patched_files():
    spec = swebench.build_test_spec(
        {
            "FAIL_TO_PASS": json.dumps(["test_issue_24211"]),
            "PASS_TO_PASS": "[]",
            "test_patch": "+++ b/sympy/physics/units/tests/test_quantities.py\n",
        }
    )
    assert spec.targets == ["sympy/physics/units/tests/test_quantities.py"]


def test_django_runs_its_own_runner_on_the_patched_modules():
    spec = swebench.build_test_spec(
        {
            "repo": "django/django",
            "FAIL_TO_PASS": json.dumps(["test_x (admin_views.tests.AdminViewTests)"]),
            "PASS_TO_PASS": "[]",
            "test_patch": "+++ b/tests/admin_views/tests.py\n",
        }
    )
    assert spec.kind == "django"
    assert spec.command.endswith("admin_views.tests")


def test_filtering_keeps_source_and_drops_everything_test_shaped():
    diff = (
        _new_file_diff("pkg/core.py", "x = 1")
        + _new_file_diff("pkg/tests/test_core.py", "def test(): pass")
        + _new_file_diff("conftest.py", "x = 1")
        + _new_file_diff("pytest.ini", "[pytest]")
        + _new_file_diff("setup.cfg", "[tool:pytest]\naddopts = -p no:x")
        + _new_file_diff("pyproject.toml", "[project]\nname = 'x'")
    )
    kept, dropped = filter_source_diff(diff)
    assert "pkg/core.py" in kept and "pyproject.toml" in kept
    assert set(dropped) == {"pkg/tests/test_core.py", "conftest.py", "pytest.ini", "setup.cfg"}


# ------------------------------------------------------------------ docker
#
# Needs a Docker daemon and a local image, so it is opt-in:
#   CODEPILOT_DOCKER_IMAGE=swebench/sweb.eval.x86_64.pallets_1776_flask-4992:latest pytest -k docker
# Any image with python and pytest works; an official SWE-bench image is used
# through its /testbed conda environment.

DOCKER_IMAGE = __import__("os").environ.get("CODEPILOT_DOCKER_IMAGE")


@pytest.mark.skipif(not DOCKER_IMAGE, reason="set CODEPILOT_DOCKER_IMAGE to run")
async def test_docker_backend_grades_the_same_way_with_no_network(tmp_path):
    instance, _ = make_task(tmp_path)
    env = await BenchEnv.create(
        instance["repo_url"], instance["base_commit"], backend="docker",
        install=False, image=DOCKER_IMAGE, task_id="calc-docker",
    )
    try:
        offline = await env.run(
            'python -c "import socket; socket.create_connection((\'1.1.1.1\', 53), 3)"', timeout=30
        )
        assert offline.exit_code != 0, "the agent's container still has a network"
        assert (await swebench.grade(env, instance, GOLD_PATCH)).resolved
        assert not (await swebench.grade(env, instance, "")).resolved
    finally:
        await env.close()
