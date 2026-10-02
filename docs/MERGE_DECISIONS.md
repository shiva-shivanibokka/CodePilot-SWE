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

## D5. Local sandbox: process-tree kill on timeout (bug reproduced, fixed)

**What.** `codepilot/sandbox/local.py` now runs commands through
`_run_with_deadline` / `_kill_tree`, ported from
`B:sandbox/local_workspace.py`. A timed-out command reports exit 124 (as
coreutils `timeout` and the Docker backend do) instead of -1.

**Reproduced first.** A command whose child spawns a grandchild that keeps
stdout open, run with `timeout_seconds=2`, took **21.2 s** with A's
`subprocess.run(timeout=...)` (scratch script), and **31.3 s** in
`tests/test_local_sandbox.py::test_a_timeout_kills_the_whole_process_tree`
before the fix. After: passes in under 3 s.

**Compared.** A's sandbox had interpreter normalisation and was already the
agent's backend; B's had the tree kill, env scrubbing and a bash requirement.
A's class is kept and gains B's three behaviours as options
(`scrub_secrets`, `shell`, `python`, `path_prefix`) so the interactive CLI's
behaviour is unchanged by default. B's `/repo` path virtualisation is **not**
ported: CodePilot's tools use repository-relative paths, so there is nothing
to translate.

**Tests.** `tests/test_local_sandbox.py` (5 tests; the scrubbing test is B's
`test_provider_keys_are_stripped_from_the_child_environment`, rewritten
against the new class).

## D6. Denylist gains B's local-backend refusals (gap reproduced, fixed)

**What.** `DEFAULT_DENIED` in `codepilot/permissions.py` gains `sudo`,
`wget … | sh`, `rm -r ~` / `$HOME`, `halt`, `poweroff`.

**Reproduced first.** B's refusal cases, ported as
`tests/test_permissions.py::test_catastrophic_commands_are_refused_even_when_auto_approved`:
4 of 8 were *allowed* under `auto_approve=True` before the change (`sudo pip
install foo`, `wget … | sh`, `rm -rf ~`, `halt`). The benchmark auto-approves,
so on the no-Docker backend these would have run on the host. B's
"ordinary commands are allowed" cases are ported too, so the list did not grow
into blocking normal work (`rm -rf build/` still runs).

## D7. `read_file` takes a line range

**What.** Optional `start_line` / `end_line` on `read_file`.

**Why.** `_truncate` keeps the head and tail of long output, so the middle of a
file longer than ~12k characters could not be read at all. SWE-bench
repositories routinely have files of several thousand lines. B's editor had
`view_range` for this (`B:agent/tools/editor.py::_view`).

**Reproduced first.** `tests/test_tools.py::test_the_middle_of_a_large_file_can_be_read`
failed with "unexpected keyword argument 'start_line'"; passes after.
The ledger still hashes the whole file, so a partial read permits an exact-
string edit (`test_a_partial_read_still_allows_an_edit`).

## D8. BM25 search ported as `search_code`

**What.** `B:agent/tools/search.py` moved (with history) to
`codepilot/search_index.py` and rewritten; registered as the `search_code`
tool beside A's regex `search`.

**Kept from B.** BM25 over 30-line chunks; the cache keyed by file pattern
(B's own regression fix, `B:tests/test_regressions.py::TestSearchIndexCacheKey`).

**Changed, with reasons.**
- Files come from `Workspace.list_files()` and are read directly, instead of
  one `cat` subprocess per file through a POSIX shell.
- Embedding blend dropped: B used sentence-transformers only if installed,
  which makes ranking depend on the machine. Reproducibility of benchmark
  runs outweighs it; nothing in B measured the embedding half's benefit.
- `BM25Plus` instead of `BM25Okapi`. Reproduced: with Okapi, a one-chunk
  corpus (a narrow `file_pattern`, a small repo) scored the exact match 0 and
  the tool answered "No results" — the first run of
  `test_search_code_does_not_answer_from_files_as_they_were` failed that way.
- The cache is cleared by every edit. In B the index was built once per task
  and only cleared at teardown (`B:agent/loop.py`, `finally: clear_index`), so
  searches after an edit answered from pre-edit contents (from reading the
  code; covered by the test above).

**Effect on A's old experiments.** `search_code` is a new tool, so a
configuration that passes `tool_names=None` (all tools) now offers one more
tool than when A's committed results were measured. The experiments that
restrict the tool set (edit-style, retrieval) are unaffected.

Note on D8: the rewrite of `search_index.py` exceeds git's rename-similarity
threshold, so `git log --follow codepilot/search_index.py` stops at the merge.
The original is `swe/agent/tools/search.py` at commit `69fe4a9`.

## D9. Benchmark grading: three leaks closed, each reproduced first

All three were reproduced against B's code extracted unmodified from this
repository's history (`git archive 9fd0895`) into a scratch directory, run
with `PYTHONDONTWRITEBYTECODE=1` so nothing was written beside it.

1. **Clone-history leakage.** On a two-commit fixture (base, then the fix),
   `B:sandbox/workspace.py::clone_repo(url, base, dest)` produced a checkout
   where `git cat-file -e <fix>` succeeded, `git remote -v` listed `origin`,
   and `git log --all` printed the fix commit. Replaced by
   `codepilot/bench/checkout.py::clone_at`: `git init` + `fetch --depth=1` of
   the one commit by URL, `FETCH_HEAD` deleted, then `assert_isolated` (no
   remotes, every ref inside HEAD's history). Proof:
   `tests/test_bench_checkout.py::test_the_gold_fix_commit_is_unreachable`
   and three neighbours.
2. **The 20-test cap.** `B:eval/harness.py::build_test_command` with 25 node
   ids put 20 in the command (`MAX_GRADED_TESTS = 20`). Removed; proof:
   `tests/test_bench_grading.py::test_every_required_test_is_graded_no_cap`
   (25th id is a missing test, and the instance fails).
3. **`-k` substring matching.** With `FAIL_TO_PASS = ["test_issue_1"]` and a
   file containing only a passing `test_issue_10`, B's command
   `pytest tests/test_m.py -k "test_issue_1" -x` ran `test_issue_10`, exited
   0, and graded RESOLVED. Replaced by running the target files whole with
   `-rA -v`, parsing per-test outcomes (`codepilot/bench/testlog.py`) and
   looking up every id exactly (`testlog.lookup`). Proof:
   `test_a_required_test_that_never_ran_is_a_failure_not_a_pass` and
   `tests/test_bench_testlog.py`.

Also from the same review of `grade()`:

4. **`-x` in grading and validation.** Reproduced for validation: with one
   pre-existing failure collected first, B's regression command
   (`pytest -x -q`) measured `(passed, failed, errors) = (0, 1, 0)` for both a
   correct candidate and one that breaks another test — indistinguishable, and
   both rejected by B's `passed > 0` rule. No `-x` anywhere in the merged
   benchmark (`test_the_graded_command_has_no_exitfirst_no_k_and_no_cap`).
5. **Grading in the agent's tree.** B applied the test patch on top of the
   agent's working tree; A's eval wrote held-out tests into it. Now
   `swebench.grade` restores the checkout to its baseline
   (`checkout.restore_pristine`), applies only the agent's diff filtered by
   `codepilot/bench/grading.py::filter_source_diff` (drops test files,
   `conftest.py`, pytest config, start-up hooks, and files the test patch
   touches), then the test patch. Proof:
   `test_an_agent_written_conftest_cannot_flip_the_result` first shows the
   conftest attack *works* when live (the held-out test reports PASSED with no
   fix), then that grading does not resolve. Also
   `test_a_conftest_hidden_in_an_ignored_directory_is_removed` and
   `test_an_agent_that_deletes_the_failing_test_gains_nothing`. Positive
   control: `test_the_gold_patch_resolves`.

Django support (`testlog.parse_django`, `build_test_spec` running
`tests/runtests.py --verbosity 2`) follows SWE-bench's own approach and is
unit-tested on log text only; no Django instance has been run end to end here.

**Kept from B unchanged:** the HTTP dataset loader and the difficulty-label
loader (`codepilot/bench/swebench.py`).

## D10. One benchmark workspace interface

**What.** `codepilot/bench/environment.py::BenchEnv` replaces B's
`LocalWorkspace` / `DockerWorkspace` pair and B's `/repo` path convention. The
checkout is always on the host (so CodePilot's `Workspace` and the git-based
diff/restore/grading are backend-independent); commands go through
CodePilot's `Sandbox` protocol — `LocalSandbox` (per-task venv, bash, keys
scrubbed) or `DockerSandbox` (checkout bind-mounted).

**Compared.** B's Docker backend copied the repo into the container with tar
and ran `pip install -e .` inside a container started with `network_mode=
"none"` (`B:sandbox/docker_workspace.py`, `_setup_repo`), so the install could
not download anything; B's own `--backend local` was what its recordings used.
Here the container starts on a network for setup and is disconnected
(`DockerSandbox.disconnect_network`) before the agent's first command. With an
official SWE-bench image the checkout is mounted over `/testbed` and the
image's conda env is put first on PATH.

**Evidence.** `tests/test_bench_grading.py::test_docker_backend_grades_the_same_way_with_no_network`
(opt-in; run here with the locally present
`swebench/sweb.eval.x86_64.pallets_1776_flask-4992` image: a socket connect
from inside fails, the gold patch resolves, the empty patch does not). The
first attempt used conda's `activate` script, which does not run under `sh`;
the test caught it (`No module named pytest`) and PATH selection replaced it.

**Ported from B's local backend into `LocalSandbox`** (D5): tree kill,
scrubbing, bash. **Not ported:** B's `/repo` virtual root and output path
rewriting (no longer needed), B's tar-based file I/O (the checkout is on the
host).

## D11. Workspace writes keep the file's line endings (Windows bug, reproduced)

**What.** `Workspace.write` and `Workspace.edit` write back with the file's
existing line ending (`_newline_of`); new files get `\n`.

**Reproduced first.** `tests/test_workspace.py::test_an_edit_preserves_lf_line_endings`
and `test_writing_a_new_file_writes_exactly_what_was_given` failed on this
Windows machine: `read_text` normalises to `\n` and `write_text` translates
every `\n` to `\r\n`, so one edit rewrote every line of an LF file
(`b'a = 1\r\nb = 3\r\n'`). On Linux the tests passed before and after, which is
why A's Linux CI never saw it. Found while making agentless candidates go
through the same `Workspace` writes as the agent's edits.

**Related.** Benchmark checkouts set `core.autocrlf=false` locally, so the
working tree holds the repository's bytes regardless of the machine's global
setting (this machine checks LF files out as CRLF: `file` reported CRLF for
A's own sources right after the clone in D1).
`tests/test_bench_checkout.py::test_the_checkout_holds_the_repositorys_bytes_whatever_autocrlf_says`.
