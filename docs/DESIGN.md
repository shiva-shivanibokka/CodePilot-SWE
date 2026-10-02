# Design

The decisions that shape this repository, and why. Sections 1–5 are folded
from CodePilot-Agent's design spec
(`docs/superpowers/specs/2026-09-01-codepilot-agent-design.md`, removed in the
merge; `git show 83d06d8:docs/superpowers/specs/2026-09-01-codepilot-agent-design.md`
for the original, including its milestone plan). Section 6 is the merge.
Where the merge overturned a decision, the original is kept and marked.

## 1. Where it started

CodePilot-Agent first presented as a six-agent LangGraph system. An audit on
2026-09-01 found it had never run: `KeyError: '_router'` on the first node
transition, a plan pointer that never advanced, an unbounded coder/reviewer
loop, agents with no tools and no memory of each other, and a model id that
had never existed. The rebuild's goal: a small but architecturally honest
coding agent that is both a tool its author uses and a measurable artefact,
with every published number backed by a committed results file.

## 2. Decisions (ADRs)

**ADR-1 — Two arms, sharing everything except control flow.** A free tool
loop (`agent/loop.py`) and a fixed plan→code→test→debug→review graph
(`agent/pipeline.py`) share the client, tools, sandbox, context and event
stream, so comparing them measures control flow and nothing else. The merge
applies the same rule to the SWE-bench arms (section 6).

**ADR-2 — Anthropic only, behind one seam.** *Superseded by the merge.* The
original reasoning: prompt caching is the dominant cost lever and is
Anthropic-shaped, and an abstraction over every provider tends to drop it. The
merge replaced the client with LiteLLM but kept the seam (one module), the
Anthropic-shaped internal message format, and explicit cache breakpoints where
the provider honours them (docs/MERGE_DECISIONS.md, D2).

**ADR-3 — Edits land in the real repository, protected by git.** Before each
turn the tree is written to a scratch ref with a throwaway index; `codepilot
undo` restores it. An agent that edits a temp directory that is then deleted is
a demo.

**ADR-4 — The sandbox splits by purpose.** The local tool runs in your
working directory behind a permission gate (isolating your code from your own
machine is theatre). Anything executing model-written code against code you
did not write — the evals, the benchmark, hosted mode — belongs in a container.

**ADR-5 — The event stream is the public interface.** Every arm emits the same
events; the CLI renders them, sessions persist them, the replay page plays
them, and (since the merge) the Prometheus metrics subscribe to them.

**ADR-6 — Default model and the cost dial.** Default `claude-opus-5`; model
ids overridable by environment so a retirement is a config change. Whether
lowering effort beats routing to a weaker model was made an experiment
(experiment 4) rather than an assumption.

## 3. Safety model

* **Checkpointing** to `refs/codepilot/<session>/<n>`; your index and `HEAD` are
  never touched. Not a git repository → editing refused unless
  `--no-checkpoint`.
* **Read-before-write.** A file must be read this session, and unchanged since,
  before it can be edited. A refusal is a tool error the model recovers from.
* **Path containment.** Every path resolves inside the root or is refused,
  symlinks included.
* **Permission gate.** Read-only and test commands run; anything else asks; a
  denylist is refused even under auto-approve (extended in the merge, D6).
* **Budget.** Tokens, dollars and model calls capped per turn, checked before
  each call; hitting one emits a `BUDGET` event and ends the turn visibly.
* **Test-file protection** during a debugging turn.
* **Errors are results.** A failing tool returns `is_error: true`; it never
  raises out of the turn.

## 4. Context management

* A real message list, carried across turns and `--resume`.
* Fixed prefix order — tools, system prompt, `CODEPILOT.md` — with the cache
  breakpoint after the stable part; nothing volatile in front of it. A prefix
  under the model's minimum silently does not cache (measured: 2,119 tokens
  cached on neither model tried; 7,239 and 9,327 did).
* Compaction at a token threshold: older turns summarised, system prefix and
  recent turns verbatim, never splitting a tool call from its result.

## 5. Evaluation rules

Carried from the original spec, and applied to everything added since:
report run-to-run variance before claiming a gap; state N, date and model next
to every number; never average a failed or errored run in as a result (provider
outages are excluded and counted); link the raw results file from every
published claim.

## 6. The merge

The merged repository has **one agent** — CodePilot's loop — and a
**benchmark layer** (`codepilot/bench/`) built from Autonomous-SWE-Agent's
SWE-bench harness and Agentless baseline. The rules the merge followed:

* **One substrate for all arms.** The agent arm is CodePilot's loop unchanged;
  the agentless arm calls the same LLM client with the same system-prompt base
  and cache breakpoint, writes through the same `Workspace`, and both arms'
  candidates go through one selection rule and one grader.
* **The grader must not be gameable.** The checkout contains the base commit
  and nothing after it; grading restores a pristine tree and applies only the
  agent's source diff; every required test id is judged exactly; no `-x`, no
  `-k`, no cap. Each of these was a reproduced defect (MERGE_DECISIONS.md, D9).
* **One workspace interface.** The checkout is on the host; commands go through
  the `Sandbox` protocol — a local backend for machines without Docker, a
  Docker backend (official SWE-bench images supported) with no network while
  the model acts.
* **Unique behaviour survives; duplicates do not.** Every component of
  Autonomous-SWE-Agent was compared with CodePilot's counterpart before it was
  removed, and what only it did was ported (MERGE_DECISIONS.md, D5–D8, D15).
