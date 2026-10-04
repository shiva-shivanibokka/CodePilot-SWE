"""
Per-test outcomes from a test run's log, keyed by the ids SWE-bench uses.

Grading needs to know what happened to *each named test*, not whether the run
as a whole exited 0. Autonomous-SWE-Agent graded by exit code, which is why it
needed `-k` for bare test names — and `-k` is a substring match, so a required
test that did not exist at all could be "passed" by any other test whose name
contained it (`-k "test_issue_1"` runs `test_issue_10`). Parsing the log and
looking each required id up exactly removes that, the 20-test cap, and `-x`.

Two log shapes, matching how SWE-bench's own log parsers read them:

* **pytest**, run with `-rA -v`: verbose lines (`path::test PASSED [ 50%]`)
  and the short summary (`PASSED path::test`, `FAILED path::test - msg`). The
  verbose lines are what carry SKIPPED with a node id.
* **Django's runner**, run with `--verbosity 2`:
  `test_x (module.Class) ... ok | FAIL | ERROR | skipped '…' | expected failure`.
"""

from __future__ import annotations

import re

PASSED, FAILED, ERROR, SKIPPED, XFAIL, XPASS = (
    "PASSED", "FAILED", "ERROR", "SKIPPED", "XFAIL", "XPASS",
)
#: As in SWE-bench's grader: a required test counts as passing when it passed
#: or failed in the way it was marked to.
PASSING = {PASSED, XFAIL}
STATUSES = (PASSED, FAILED, ERROR, SKIPPED, XFAIL, XPASS)

_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_VERBOSE = re.compile(
    r"^(?P<id>\S.*?::\S.*?) (?P<status>PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)"
    r"(?:\s+\(.*?\))?(?:\s+\[\s*\d+%\])?\s*$"
)
_SUMMARY = re.compile(
    r"^(?P<status>PASSED|FAILED|ERROR|XFAIL|XPASS) (?P<id>\S.*?::\S.*?)(?: - .*)?$"
)
_DJANGO = re.compile(r"^(?P<name>\w+) \((?P<where>[\w.]+)\)(?P<rest>.*)$")
_DJANGO_RESULT = re.compile(r"\s\.\.\.\s(?P<outcome>.*)$")
_DJANGO_OUTCOME = {
    "ok": PASSED,
    "FAIL": FAILED,
    "ERROR": ERROR,
    "expected failure": XFAIL,
    "unexpected success": XPASS,
}


def parse_pytest(log: str) -> dict[str, str]:
    """node id -> status. A summary line overrides a verbose line for the same
    id, because the summary is written after teardown errors are known."""
    statuses: dict[str, str] = {}
    summary: dict[str, str] = {}
    for raw in log.splitlines():
        line = _ANSI.sub("", raw).rstrip()
        m = _SUMMARY.match(line)
        if m:
            summary[m.group("id").strip()] = m.group("status")
            continue
        m = _VERBOSE.match(line)
        if m:
            statuses[m.group("id").strip()] = m.group("status")
    statuses.update(summary)
    return statuses


def parse_django(log: str) -> dict[str, str]:
    """'test_x (module.Class)' -> status, and the docstring form beside it.

    Python 3.11+ unittest prints the description as `(module.Class.test_x)`;
    the trailing method name is stripped so ids match SWE-bench's.

    A test with a docstring prints the docstring's first line in place of a
    description, and **that line is the id SWE-bench's own parser records**
    (D46) — 136 of the required ids of the Django instances in the study's
    first 50 are docstrings. Such a test is recorded under both forms: the
    docstring, so the dataset's id matches, and `name (where)`, so a caller
    holding the name form still finds it.
    """
    statuses: dict[str, str] = {}
    pending: tuple[str, str] | None = None
    for raw in log.splitlines():
        line = _ANSI.sub("", raw).rstrip()
        m = _DJANGO.match(line)
        described = None
        if m:
            pending = (m.group("name"), m.group("where"))
            rest = m.group("rest")
        elif pending is not None:
            # A test with a docstring prints it on the next line, before
            # " ... ok", and that docstring is the id the dataset uses.
            rest = line
            described = _DJANGO_RESULT.sub("", line).strip()
        else:
            continue
        result = _DJANGO_RESULT.search(rest)
        if not result:
            continue
        name, where = pending
        pending = None
        outcome = result.group("outcome").strip()
        if where.endswith("." + name):
            where = where[: -len(name) - 1]
        status = _DJANGO_OUTCOME.get(outcome)
        if status is None and outcome.startswith("skipped"):
            status = SKIPPED
        if status is not None:
            statuses[f"{name} ({where})"] = status
            if described:
                statuses[described] = status
    return statuses


def lookup(statuses: dict[str, str], test_id: str) -> str | None:
    """The status of one required test, by exact id, or None if it never ran.

    Node ids (`path::name[param]`) and Django ids match exactly. A bare name
    (sympy's FAIL_TO_PASS lists `test_issue_24211`) matches tests whose final
    `::` component is exactly that name — never a prefix or substring. If the
    same bare name appears in several files the worst outcome wins, so one
    passing copy cannot hide a failing one.
    """
    if test_id in statuses:
        return statuses[test_id]
    if "::" in test_id or " (" in test_id:
        return None
    found = [s for nid, s in statuses.items() if nid.rsplit("::", 1)[-1] == test_id]
    if not found:
        return None
    for worst in (ERROR, FAILED, XPASS, SKIPPED, XFAIL, PASSED):
        if worst in found:
            return worst
    return found[0]
