"""Pulling one JSON value out of a model's reply.

Moved verbatim from Autonomous-SWE-Agent's `agent/llm.py::extract_json`, where
it was the only part of that module the agentless pipeline needed once the
client itself was replaced. Its regression tests moved with it
(`tests/test_agentless.py`).
"""

from __future__ import annotations

import json
from typing import Any


def extract_json(text: str, expect: type | None = None) -> Any:
    """
    Pull the first complete JSON value out of a model response.

    Models wrap JSON in prose, in ```json fences, or in both, and the usual
    fix — regex the fences off, then `json.loads` — breaks the moment the JSON
    itself contains a fence or a brace inside a string. Scanning for a position
    where the decoder can read a whole value handles every one of those without
    special cases.

    Args:
        text:   The raw model output.
        expect: Optional type the value must be, e.g. dict. Guards against
                returning a nested fragment that happens to parse first.
    """
    decoder = json.JSONDecoder()
    for start, char in enumerate(text):
        if char not in "{[":
            continue
        try:
            value, _ = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            continue
        if expect is not None and not isinstance(value, expect):
            continue
        return value
    raise ValueError(f"no JSON {expect.__name__ if expect else 'value'} found in response")
