"""The agentless baseline (Xia et al., 2024): localise, then sample patches.

Choosing among the samples is `codepilot.bench.selection`, shared with the
agent arm.
"""

from .localize import LocalizationResult, localize
from .pipeline import AgentlessRun, run_agentless
from .repair import RepairResult, apply_search_replace, repair

__all__ = [
    "AgentlessRun",
    "LocalizationResult",
    "RepairResult",
    "apply_search_replace",
    "localize",
    "repair",
    "run_agentless",
]
