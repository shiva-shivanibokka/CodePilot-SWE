"""Reading per-test outcomes out of real-shaped logs."""

from __future__ import annotations

from codepilot.bench import testlog

PYTEST_LOG = """\
============================= test session starts =============================
collected 5 items

sympy/physics/units/tests/test_quantities.py::test_str_repr PASSED       [ 20%]
sympy/physics/units/tests/test_quantities.py::test_issue_24211 FAILED    [ 40%]
sympy/physics/units/tests/test_quantities.py::test_issue_242 PASSED      [ 60%]
tests/test_config.py::test_param[a b] PASSED                             [ 80%]
tests/test_config.py::TestX::test_skip SKIPPED (no toml)                 [100%]

=========================== short test summary info ============================
PASSED sympy/physics/units/tests/test_quantities.py::test_str_repr
PASSED sympy/physics/units/tests/test_quantities.py::test_issue_242
PASSED tests/test_config.py::test_param[a b]
FAILED sympy/physics/units/tests/test_quantities.py::test_issue_24211 - AssertionError: x
========================= 1 failed, 3 passed, 1 skipped in 0.42s ==============
"""

DJANGO_LOG = """\
test_add (admin_views.tests.AdminViewTests) ... ok
test_change (admin_views.tests.AdminViewTests.test_change) ... FAIL
test_docs (admin_views.tests.AdminViewTests)
Docstring first line. ... ERROR
test_old (admin_views.tests.AdminViewTests) ... skipped 'old'
test_known (admin_views.tests.AdminViewTests) ... expected failure
"""


def test_pytest_verbose_and_summary_lines_are_both_read():
    s = testlog.parse_pytest(PYTEST_LOG)
    assert s["sympy/physics/units/tests/test_quantities.py::test_issue_24211"] == "FAILED"
    assert s["tests/test_config.py::test_param[a b]"] == "PASSED"
    assert s["tests/test_config.py::TestX::test_skip"] == "SKIPPED"


def test_a_bare_name_matches_exactly_never_by_substring():
    s = testlog.parse_pytest(PYTEST_LOG)
    assert testlog.lookup(s, "test_issue_24211") == "FAILED"
    assert testlog.lookup(s, "test_issue_242") == "PASSED"
    assert testlog.lookup(s, "test_issue_2") is None
    assert testlog.lookup(s, "test_issue") is None


def test_the_worst_outcome_wins_when_a_bare_name_is_ambiguous():
    s = {"a/test_x.py::test_same": "PASSED", "b/test_y.py::test_same": "FAILED"}
    assert testlog.lookup(s, "test_same") == "FAILED"


def test_a_node_id_never_falls_back_to_a_bare_name_match():
    s = {"a/test_x.py::test_same": "PASSED"}
    assert testlog.lookup(s, "b/test_y.py::test_same") is None


def test_django_outcomes_including_docstrings_and_new_style_ids():
    s = testlog.parse_django(DJANGO_LOG)
    assert s == {
        "test_add (admin_views.tests.AdminViewTests)": "PASSED",
        "test_change (admin_views.tests.AdminViewTests)": "FAILED",
        "test_docs (admin_views.tests.AdminViewTests)": "ERROR",
        "test_old (admin_views.tests.AdminViewTests)": "SKIPPED",
        "test_known (admin_views.tests.AdminViewTests)": "XFAIL",
    }
