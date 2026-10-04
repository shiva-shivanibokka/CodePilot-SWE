"""Prices: pinned, explicit, and never silently zero."""

from __future__ import annotations

import subprocess
import sys


def test_importing_codepilot_pins_litellm_to_its_bundled_cost_map():
    """Without LITELLM_LOCAL_MODEL_COST_MAP, LiteLLM downloads its cost map
    from GitHub at import, so the prices a run used depend on when it ran."""
    out = subprocess.run(
        [sys.executable, "-c",
         "import os, codepilot, litellm; print(os.environ.get('LITELLM_LOCAL_MODEL_COST_MAP'))"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert out == "True"


def test_the_installed_litellm_is_the_pinned_one():
    from importlib.metadata import version

    assert version("litellm") == "1.103.2"
