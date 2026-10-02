"""
The agentless arm: localise, sample N patches, hand them to selection.

Phase 1 (`localize.py`) and phase 2 (`repair.py`) are Autonomous-SWE-Agent's
pipeline on CodePilot's client and shared prompt. Phase 3, validation and
choice, is no longer here: it is `codepilot/bench/selection.py`, shared with
the agent arm, so the two arms cannot differ in how a patch is chosen.

Each sampled edit is written into the pristine checkout through CodePilot's
`Workspace` (so line endings are preserved exactly as for the agent's edits)
and captured as a git diff, the same artefact the agent arm produces.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from codepilot.bench.agentless.localize import LocalizationResult, localize
from codepilot.bench.agentless.repair import RepairResult, repair
from codepilot.bench.selection import Candidate
from codepilot.llm import Usage
from codepilot.workspace import Workspace


@dataclass
class AgentlessRun:
    candidates: list[Candidate]
    localization: LocalizationResult
    repair: RepairResult
    notes: list[str] = field(default_factory=list)

    @property
    def usage(self) -> Usage:
        return self.localization.usage + self.repair.usage

    @property
    def cost_usd(self) -> float:
        return self.localization.cost_usd + self.repair.cost_usd

    @property
    def calls(self) -> int:
        return self.localization.calls + self.repair.calls

    @property
    def unpriced_calls(self) -> int:
        return self.localization.unpriced_calls + self.repair.unpriced_calls


async def run_agentless(env, client, model: str | None, issue: str, num_samples: int,
                        seed: int | None = None) -> AgentlessRun:
    env.restore()
    files = Workspace(root=env.root).list_files()
    loc = await localize(client, model, env.root, files, issue,
                         seed=None if seed is None else seed * 1000 + 999)
    rep = await repair(client, model, env.root, issue, loc, num_samples, seed=seed)

    candidates: list[Candidate] = []
    notes: list[str] = []
    if not loc.suspect_files and not loc.suspect_locations:
        notes.append("localisation named no file that exists in the repository")
    for sample in rep.samples:
        if sample.patched is None:
            notes.append(f"sample {sample.index + 1} rejected: {sample.note}")
            continue
        env.restore()
        ws = Workspace(root=env.root)
        ws.read(sample.path)
        ws.write(sample.path, sample.patched)
        candidates.append(
            Candidate(
                index=sample.index,
                diff=env.diff(),
                origin=f"agentless sample {sample.index + 1} ({sample.path}, t={sample.temperature})",
            )
        )
    env.restore()
    return AgentlessRun(candidates, loc, rep, notes)
