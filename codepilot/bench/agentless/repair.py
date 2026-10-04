"""
Agentless phase 2 — sample candidate patches.

For the localised file(s), ask the model N times — no tools, the file in the
prompt — for one search/replace edit each. Phase 3 (`selection.py`) chooses
among them.

Ported from Autonomous-SWE-Agent. Kept: the search/replace output format and
`apply_search_replace` with its reasons for every rejection, the
de-duplication of repeated locations, and the one re-ask when a reply hits the
token cap. Changed:

* **N means N.** The original gave each location `num_samples // locations`
  samples, so 10 samples over 3 locations was 9. Samples are now dealt round
  robin across locations, so a budget-matched comparison (N agent attempts vs
  N samples) compares N with N.
* Each candidate becomes a **git diff of the checkout**, the same artefact the
  agent arm produces, so both arms go through one selection and one grader.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from codepilot.bench.agentless.jsonx import extract_json
from codepilot.bench.agentless.localize import LocalizationResult
from codepilot.bench.prompts import REPAIR_ARM, issue_message, system
from codepilot.llm import LLMError, Usage

#: Room for one search/replace pair. Doubled once on a reply that runs out.
SAMPLE_MAX_TOKENS = int(os.getenv("AGENTLESS_SAMPLE_MAX_TOKENS", "4096"))
MAX_LOCATIONS = 3


@dataclass
class Sample:
    index: int
    path: str
    patched: str | None
    note: str  # the model's explanation, or why the sample was rejected
    temperature: float


@dataclass
class RepairResult:
    samples: list[Sample]
    usage: Usage = field(default_factory=Usage)
    cost_usd: float = 0.0
    calls: int = 0
    unpriced_calls: int = 0
    retried: int = 0

    @property
    def rejected(self) -> list[str]:
        return [s.note for s in self.samples if s.patched is None]


def locations_for(loc: LocalizationResult) -> list[dict]:
    """The places to sample at: the top localised locations, de-duplicated.

    Localisation routinely names the same function twice — once per reason it
    found it — and each duplicate would otherwise take a share of the samples.
    """
    spots = loc.suspect_locations[:MAX_LOCATIONS] or [
        {"file": f, "function_name": None, "class_name": None} for f in loc.suspect_files[:2]
    ]
    unique = {
        (s.get("file", ""), s.get("class_name") or "", s.get("function_name") or ""): s
        for s in spots
        if s.get("file")
    }
    return list(unique.values())


def _hint(spot: dict) -> str:
    fn, cls = spot.get("function_name"), spot.get("class_name")
    if not fn:
        return ""
    return f"\nFocus on the `{cls}.{fn}` method." if cls else f"\nFocus on the `{fn}` function."


def temperature_for(index: int) -> float:
    """The sampling schedule both arms share: near-greedy first, then diverse."""
    return 0.2 if index == 0 else 1.0


async def repair(client, model: str | None, root, issue: str, loc: LocalizationResult,
                 num_samples: int, seed: int | None = None) -> RepairResult:
    result = RepairResult(samples=[])
    spots = locations_for(loc)
    contents: dict[str, str] = {}
    for spot in spots:
        try:
            contents[spot["file"]] = (root / spot["file"]).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
    spots = [s for s in spots if s["file"] in contents]
    if not spots:
        return result

    for index in range(num_samples):
        spot = spots[index % len(spots)]
        path = spot["file"]
        temperature = temperature_for(index)
        # One seed per sample: the same seed with the same prompt makes
        # temperature-1 samples identical (found on the SOP eval branch of
        # Autonomous-SWE-Agent, 9911b2d). The length re-ask reuses it.
        sample_seed = None if seed is None else seed * 1000 + 500 + index
        prompt = (
            issue_message(issue)
            + f'\n\n<file path="{path}">\n{contents[path]}\n</file>\n'
            + _hint(spot)
        )
        try:
            reply = await _ask(client, model, prompt, temperature, SAMPLE_MAX_TOKENS, result,
                               sample_seed, f"sample-{index}")
        except LLMError as exc:
            if "context overflow" not in str(exc):
                raise
            # The file does not fit the model's window: this sample is lost,
            # the others (other locations) may not be.
            result.samples.append(Sample(index, path, None, str(exc), temperature))
            continue
        if reply.stop_reason == "max_tokens":
            # A reply cut off mid-JSON is unusable and was paid for in full.
            # Asking once more with room to finish costs one call; discarding
            # it buys nothing.
            try:
                reply = await _ask(client, model, prompt, temperature, SAMPLE_MAX_TOKENS * 2,
                                   result, sample_seed, f"sample-{index}-reask")
            except LLMError as exc:
                if "context overflow" not in str(exc):
                    raise
                result.samples.append(Sample(index, path, None, str(exc), temperature))
                continue
            result.retried += 1
        patched, note = apply_search_replace(contents[path], reply.text, reply.stop_reason)
        result.samples.append(Sample(index, path, patched, note, temperature))
    return result


async def _ask(client, model, prompt, temperature, max_tokens, result: RepairResult, seed=None,
               tag: str = ""):
    reply = await client.chat(
        [{"role": "user", "content": prompt}],
        system=system(REPAIR_ARM),
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        **({"seed": seed} if seed is not None else {}),
        cache_tag=f"agentless:{tag}",
    )
    result.usage = result.usage + reply.usage
    result.calls += 1
    if reply.cost_usd is None:
        result.unpriced_calls += 1
    else:
        result.cost_usd += reply.cost_usd
    return reply


def apply_search_replace(file_content: str, text: str, stop_reason: str | None = None) -> tuple[str | None, str]:
    """
    Turn one sampled search/replace pair into patched file content.

    Returns (patched_content, explanation) on success and (None, reason) on
    failure. Every rejection carries a reason: a phase that yields no patches
    needs to be able to say whether the model was truncated, hallucinated the
    lines, or matched in more than one place.
    """
    if stop_reason == "max_tokens":
        return None, "response hit the token cap before the JSON closed"

    try:
        parsed = extract_json(text, expect=dict)
    except ValueError:
        return None, "no JSON object in the response"

    search = parsed.get("search") or ""
    replace = parsed.get("replace")
    explanation = str(parsed.get("explanation") or "")

    if not search or replace is None:
        return None, "missing 'search' or 'replace'"

    occurrences = file_content.count(search)
    if occurrences == 0:
        return None, "'search' does not appear in the file"
    if occurrences > 1:
        return None, f"'search' matches {occurrences} places, not one"

    patched = file_content.replace(search, replace, 1)
    if patched == file_content:
        return None, "the patch changes nothing"
    return patched, explanation
