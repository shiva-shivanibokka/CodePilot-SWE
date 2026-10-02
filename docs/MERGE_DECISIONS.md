# Merge decisions

One entry per change made while merging **CodePilot-Agent** (repo A, the base)
and **Autonomous-SWE-Agent** (repo B) into this repository. Each entry says what
changed, why, the evidence, and what was preserved. Entries are in the order
the work was done; later entries can revise earlier ones and say so.

Conventions: `A:` and `B:` paths refer to the source repositories at the
commits that were imported (A `88a836f`, B `9fd0895`). Plain paths refer to
this repository.

Baselines measured before any change, on this machine (Windows 11, Python
3.12.3):

- A: `pytest tests` -> **193 passed** (system interpreter, 295 s).
- B: `pytest tests` -> **90 passed, 3 failed**. All three failures are
  `ModuleNotFoundError: github` in `tests/test_harness.py::TestGithubUrlParser`
  — PyGithub was not installed in that interpreter. Not a code defect.

---

## D1. History import

**What.** `git clone` of A's `main` (24 commits), `git remote remove origin`.
B's `main` (27 commits) fetched from the local path into a temporary ref, every
top-level path moved under `swe/` in one pure-rename commit, then merged with
`--allow-unrelated-histories`. The temporary branch, ref and `swe` remote were
removed afterwards. No remote of any kind is configured.

**Why.** Both projects' history must survive. A pure-rename commit (rather than
`git subtree`) keeps `git log --follow` working through the later moves into
`codepilot/`.

**Evidence.** `git log --oneline | wc -l` = 53 (24 + 27 + rename + merge).
`git log --follow -- swe/agentless/validate.py` reaches B's `d419647`.
`git remote -v` is empty.

**Disclosure.** The first attempt created doubled paths (`swe/agent/agent/…`)
because the target directories were pre-created before `git mv`. That attempt
was discarded with `git reset --hard 88a836f` and `git branch -D swe-move`
inside this new repository, *before* the no-destructive-git rule was given.
Nothing outside this repository was touched; the two source repositories were
only read (`git fetch` from them reads; it writes nothing to them).

## D2. Provider seam: Anthropic SDK -> LiteLLM

**What.** `codepilot/llm.py` rewritten on LiteLLM. The public surface
(`LLMClient.chat/validate/count_tokens`, `Reply`, `Usage`, `ToolCall`,
`LLMError`, `load_env`, `price_of`, the model constants) is unchanged in
signature, plus an optional `temperature` on `chat`.

**Why.** Task requirement: run on any provider, including free-tier Groq and
Gemini. B already did this with LiteLLM (`B:agent/llm.py`).

**Compared before replacing.**

| behaviour | A (`A:codepilot/llm.py`) | B (`B:agent/llm.py`) | kept |
|---|---|---|---|
| providers | Anthropic only | any LiteLLM route | B |
| message shape | Anthropic content blocks, round-tripped | OpenAI dicts | A's shape internally; translation at the seam (below) |
| cache breakpoints | `cache_control` on last system block | none | A, passed through for Anthropic, stripped elsewhere |
| usage | uncached input + cache read/write | prompt/completion only | A's semantics, computed from LiteLLM usage |
| cost of unknown model | `None` ("unknown", not free) | `0.0` | A — B's 0.0 makes an unpriced run look free |
| errors | NotFound/Auth/Permission -> `LLMError`; SDK retries 429/5xx | every exception -> `LLMError` (no retry) | A's mapping; own retry loop for 429/5xx |
| model validation | `models.list()` | none | key-presence check via `litellm.validate_environment` (no uniform list-models API) |
| bad tool-call JSON | n/a | silently `{}` | surfaced as an unknown argument, so the tool returns "Bad arguments" |

The internal message shape stayed Anthropic-style so that `context.py`
(compaction, `_safe_cut`), `session.py` (persistence), the replay export, and
every test that scripts a `Reply` keep working unchanged. Translation lives in
`to_openai_messages` / `reply_from_response`.

**Usage arithmetic, verified in the installed library.** LiteLLM 1.103.2 adds
`cache_creation_input_tokens` and `cache_read_input_tokens` into
`prompt_tokens` for Anthropic (`litellm/llms/anthropic/chat/transformation.py`,
lines 2372-2377). They are subtracted back out so `Usage.input_tokens` keeps
meaning *uncached* input, which `Conversation.cache_report()` relies on.

**Added behaviour, each with a test in `tests/test_llm.py`.**
- A turn with tool calls is `stop_reason="tool_use"` even if the provider says
  `finish_reason="stop"` (`test_tool_calls_win_over_a_stop_label`).
- "Request too large" (Groq, request bigger than the per-minute token limit)
  and per-day quota errors are not retried
  (`test_a_request_too_large_for_the_tier_is_not_retried`).
- `effort` is sent as `reasoning_effort` only when LiteLLM says the model
  supports reasoning; otherwise nothing is sent.

**Fallback price table.** Used only when LiteLLM's cost map has no entry.
Checked: LiteLLM 1.103.2 prices `claude-opus-5`, `claude-sonnet-5`,
`claude-haiku-4-5`, `gemini/gemini-2.5-flash`, `groq/openai/gpt-oss-120b`, but
not `groq/llama-3.3-70b-versatile` (`get_model_info` raises "isn't mapped").
The Groq rows in `PRICING` are from Groq's public price list as known at
writing and are marked in the code as needing re-verification before a paid
run.

## D3. Conversation size from reply usage, not a count-tokens call

**What.** `AgentLoop` now calls `Conversation.observe(reply.usage)` after each
model call instead of `await convo.token_count(client, tools)`.

**Why.** A's `token_count` called Anthropic's `messages.count_tokens` endpoint
— an extra request per step. LiteLLM has no provider-uniform equivalent; its
`token_counter` is a local estimate. The reply's own usage is the provider's
exact count of what the request carried, so it is both free and more accurate.
`token_count` is kept and still used once after compaction (estimate).

**Evidence.** `codepilot/agent/loop.py` (the `observe` call);
`tests/test_loop.py` passes unchanged.

## D4. Provider registry moved from B

**What.** `git mv swe/agent/providers.py codepilot/providers.py`; added
`free_tier`, `provider_for_model`, `key_env_for_model`. The CLI's key check and
`codepilot doctor` now name the variable the chosen model needs instead of
always `ANTHROPIC_API_KEY`.

**Evidence.** `tests/test_llm.py::test_the_cli_names_the_key_the_chosen_model_needs`,
`tests/test_doctor.py` (unchanged, still passes: the default model is still
`claude-opus-5`, so the variable is still `ANTHROPIC_API_KEY` there).
