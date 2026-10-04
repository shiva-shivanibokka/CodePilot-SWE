"""Shared test setup.

Import codepilot before any test module imports litellm, so the pinned local
cost map is in force for the whole session (D25), and point the user-level
spend ledger at a temporary directory for every test (D41): tests must never
write to, or read spend from, the real one.
"""

import pytest

import codepilot  # noqa: F401
import codepilot.llm


@pytest.fixture(autouse=True)
def _isolated_ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(codepilot.llm, "LEDGER_DIR", tmp_path / "user-ledger")
