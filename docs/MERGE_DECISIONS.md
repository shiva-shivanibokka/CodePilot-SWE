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

## D12. Agentless arm on the shared substrate; validation replaced by shared selection

**What.** `swe/agentless/{localize,repair,pipeline}.py` moved to
`codepilot/bench/agentless/` and rewritten on CodePilot's `LLMClient`;
`swe/agentless/validate.py` removed and replaced by
`codepilot/bench/selection.py`, which both arms use. `extract_json` moved
verbatim from `B:agent/llm.py` to `codepilot/bench/agentless/jsonx.py`
(diffed against `git show 9fd0895:agent/llm.py`: identical body).

**Compared, and what changed.**

| | B | merged | evidence |
|---|---|---|---|
| repo map | `find` + one `grep` subprocess per file via bash | reads files from `Workspace.list_files()` | — |
| sample count | `num_samples // len(locations)` per location: 10 over 3 locations = 9 (by B's code, `repair.py`) | dealt round robin, exactly N | `test_budget_matched_selection_is_not_first_that_breaks_nothing` asserts 3 repair calls for N=3 |
| candidate artefact | whole patched file, written over the original | git diff of the checkout, written through `Workspace` (keeps line endings) | `test_both_arms_resolve_through_the_real_harness` |
| validation | in order, **stop at first "valid"** (`if val.valid: break`), validity from pass/fail **counts** under `-x` | every candidate evaluated; regressions by test id; majority vote over `ast`-normalised results; fallback recorded | D9 item 4; `tests/test_agentless.py::TestRegressionsByTestId` |
| regression scope | `tests_near` (B) | kept as `nearest_test_dirs` | `test_the_tests_nearest_a_changed_file_are_chosen` |

B's count rule also accepted a candidate that fixes one test and breaks
another (counts unchanged); `test_a_swap_that_nets_to_zero_is_still_caught`.
One behaviour deliberately differs from B: a run that collects no tests is
not grounds to reject a candidate (B's `passed > 0` rejected every candidate in
a directory without tests). There is then no regression evidence, and the
selection basis says so.

**Not implemented.** Agentless's reproduction-test generation. Documented in
`selection.py` and in the study plan.

B's tracing/metrics calls inside these modules were removed with them (see
D15 for observability).

## D13. Same prompt base, same caching, same sampling schedule for all arms

`codepilot/bench/prompts.py`: every request's single system block starts with
`SHARED_BASE` and carries the one cache breakpoint. The arm-specific text, the
user message, tools, turn structure and temperature are tabulated in that
module's docstring. Agent attempt k and agentless sample k use the same
temperature (0.2 for the first, 1.0 after); `AgentLoop` gained an optional
`temperature` for this. Proof: `tests/test_bench_e2e.py::test_every_arm_gets_the_same_system_prompt_base`.

Known asymmetry, stated rather than hidden: the agent's requests also carry
the tool schemas, which sit in front of the system prompt in the cached
prefix; agentless requests carry none. `effort` is `None` for every arm.

## D14. Harness, budget-matched mode, CLI

`codepilot/bench/harness.py` runs all arms on one `BenchEnv` per instance
(clone and setup once, restore before every attempt/sample/grade).
`--attempts N` gives N agent attempts vs N agentless samples, both through
`selection.select`; N=1 submits the single diff. Budget-matched means matched
in attempts, not dollars; cost is reported per arm. An attempt that errors
keeps its partial diff and records the error; provider failures are flagged
`infra_error` and excluded, as A's runner did.

`codepilot/bench/run.py` adds two model-free arms, `gold` (must resolve) and
`empty` (must not), to check the harness per instance before any money is
spent.

End-to-end proof with a scripted model and nothing else mocked:
`tests/test_bench_e2e.py` (both arms resolve; agent conftest cheat not
resolved; agent attempts go through selection; agentless selection picks the
majority of regression-free samples over an earlier breaking one).

## D16. CodePilot's task suite moved into the bench package, graded on a clean tree

**What.** `evals/{tasks,runner}.py` and `evals/fixtures/` moved (with history,
`git mv`) to `codepilot/bench/suite/`; `evals/results/` moved to
`bench/results/codepilot-suite/` (files unchanged). Imports, the CI dry-run
step, `codepilot doctor`, ruff's exclude and `.dockerignore` updated. Run as
`python -m codepilot.bench.suite.runner`.

**Bug reproduced, then fixed.** `tests/test_suite_grading.py::test_an_agent_written_conftest_does_not_pass_a_suite_task`
failed before the change: a scripted model that only wrote a forcing
`tests/conftest.py` and finished was scored `passed=True` ("2 passed") on
`empty-guard`, because the held-out file was written into the agent's tree.
`run_one` now takes the agent's diff since the fixture commit, restores the
tree, applies only the filtered source diff (`grading.filter_source_diff`,
held-out paths protected), then writes the held-out tests. Positive control:
`test_a_real_fix_still_passes`. `RunResult` gains `dropped_from_grading`.

**Effect on committed results.** A's eight results files were produced by the
old runner. Their pass/fail could in principle have been flipped this way; the
recorded `files_edited` lists can be checked for a `conftest.py`:
across all 150 committed runs, **none** lists a `conftest.py` among
`files_edited`. That field records `edit_file`/`write_file` paths (loop) or new
files (pipeline), so a conftest created through `run_command` would not appear;
the results are carried over as measured, with this caveat in the README.

## D17. GitHub integration and observability kept, under `codepilot/integrations/`

**Investigated.** `B:github_integration/` was called from two places, both
removed in this merge: `B:api/main.py` (only `fetch_issue`; `create_pr` was
exposed as a request field but never called — `git grep create_pr` finds only
the definition, the schema field and the frontend) and `B:eval/record_run.py`
(`--issue`). `B:observability/` was called from B's loop, tools, sandboxes,
agentless pipeline and API, all removed or rewritten.

**Kept, and made to work against the merged loop.**
- `git mv` to `codepilot/integrations/github/` and
  `codepilot/integrations/observability/`.
- New `codepilot/integrations/github/solve.py`: fetch an issue, check the repo
  out as a benchmark task would (HEAD, no remote), run CodePilot's loop with the
  benchmark's agent prompt, print the diff; `--open-pr` only on request.
  `tests/test_github_integration.py::test_an_issue_is_solved_by_the_merged_loop_and_a_pr_only_on_request`.
- New `observability/events.py`: Prometheus metrics fed from CodePilot's event
  stream as a subscriber, instead of calls sprinkled through the agent.
  `test_the_event_stream_drives_the_prometheus_metrics`. `AgentMetrics` gained
  an optional `registry`. The OTLP exporter import became lazy so the package
  imports without it.

**Bugs reproduced in `pr_creator._commit_and_push`, then fixed.**
1. *Shell injection from the issue title.* It ran
   ``git commit -m "{message}"`` with `shell=True`. Reproduced against B's code
   (scratch copy, push stubbed so nothing left the machine): a title of
   `x" & echo PWNED > pwned.txt & echo "` created `pwned.txt` under Windows
   `cmd.exe`. (`$(…)` did not fire on Windows, because cmd does not expand it;
   on POSIX `/bin/sh` it would.) Now argument lists throughout.
   `test_an_issue_title_cannot_run_commands` covers all three forms.
2. *Token on the command line.* It pushed to `https://{token}@github.com/…`.
   Now the token travels as an HTTP header via `GIT_CONFIG_COUNT/KEY/VALUE` in
   the child's environment, and is masked in error text.
   `test_the_token_is_never_on_a_command_line` failed before
   (`['git push https://ghp_SECRETTOKEN@github.com/o/r.git fix-7']`).
3. *PR body.* Told reviewers to `git checkout {issue_number}` (not the
   branch), and called the project "a production SWE agent benchmarked on
   SWE-bench-lite", which no committed result supports. Now names the branch
   and says the change is unreviewed model output.
   `test_the_pr_body_does_not_claim_what_was_not_measured`.

**Proposed, not done.** `create_pr` without `repo_local_path` opens a PR for a
branch nothing pushed (from reading the code; it would need GitHub to
reproduce, so it was left alone). `solve.py` always passes the path.

## D15. The rest of Autonomous-SWE-Agent: what was kept, moved, or dropped

Every file under `swe/` after D1, with its fate. All of it remains readable in
history (`git show 69fe4a9:swe/<path>`).

| B file(s) | fate | why / evidence |
|---|---|---|
| `agent/loop.py` | dropped | Duplicate loop. Compared with A's: B stops on a `<DONE>` text marker, A on an explicit `finish` tool (no parsing of free text); both have turn caps; A adds budgets, interrupts, compaction by summary, an event stream both arms share. Unique bit `changed_lines` ported to `bench/checkout.py`. |
| `agent/tools/bash.py` | dropped | A's `run_command` covers it. B's description told the model "State IS persistent across calls: cd, export … persist", while B's own Docker backend says the opposite (`B:sandbox/docker_workspace.py::run`: "Each call is a fresh shell … `cd` does not carry across calls"); not carried over. |
| `agent/tools/editor.py` | dropped | `view_range` ported as `read_file` start/end (D7). `str_replace`/`create` = A's `edit_file`/`write_file` (A adds read-before-write). `insert` and per-file `undo_edit` not ported: `edit_file` covers insertion, and A's git checkpoints cover undo. |
| `agent/tools/search.py` | ported | D8. |
| `agent/context.py` | dropped | tiktoken count + truncate-to-summary compression. A's compaction (model summary, safe cut at tool pairs, size from provider usage — D3) kept instead; B's cut-point rule (never orphan a tool result) is the same rule as A's `_safe_cut`, already tested in `tests/test_context.py`. |
| `agent/prompts.py` | folded | Its SWE-bench workflow (explore, reproduce, fix, verify, don't touch tests) is in `bench/prompts.py::AGENT_ARM`. |
| `agent/llm.py`, `agent/providers.py` | replaced / moved | D2, D4; `extract_json` moved verbatim (D12). |
| `agentless/*` | moved, rewritten | D12. |
| `eval/harness.py` | moved, rewritten | D9. |
| `eval/run_eval.py` | replaced | `codepilot/bench/run.py`. B ran instances in a 4-worker thread pool; the new runner is sequential (one environment at a time, which is also what this machine can afford). |
| `eval/record_run.py` | dropped, one piece ported | Produced replays for the frontend. Its key-shaped-string scan is ported as `bench/run.py::redact`, applied to every result row (`test_result_rows_never_carry_a_key`). |
| `sandbox/workspace.py`, `local_workspace.py`, `docker_workspace.py`, `__init__.py` | replaced | D5, D10. |
| `sandbox/Dockerfile.sandbox` | moved, adapted | `deploy/bench.Dockerfile` (tag `codepilot-bench`): build toolchain kept; `/repo`, user and safe.directory removed because the checkout is mounted and git runs on the host. |
| `github_integration/`, `observability/` | moved | D17. |
| `api/` (FastAPI + websocket) | dropped | Its only job was driving B's loop and agentless pipeline for the Next.js frontend (`B:api/main.py` imports `agent.llm`, `agent.providers`, B's sandbox). Repointing means rewriting it against a different loop and event shape — not trivial. CodePilot already has a CLI, a web UI (`webui.py`) and a hosted HTTP API (`server.py`, `http_api.py`). |
| `frontend/` (Next.js) | dropped, data kept | Consumed `api/` and B's replay format. The eight recorded runs it shipped are real measurements and were moved to `bench/results/autonomous-swe-agent-recordings/` with a label (`NOTE.md`). Scanned with `redact`: 0 key-shaped strings. `frontend/data/benchmark.json` was `[]` (no benchmark had been run), so nothing was lost. |
| `Dockerfile`, `Dockerfile.serve`, `docker-compose.yml`, `prometheus.yml`, `requirements-serve.txt` | dropped | All serve `api/` (`CMD uvicorn api.main:app`). |
| `pyproject.toml` | dropped | Its dependencies that are still used are in `requirements.txt` (litellm, rank-bm25, PyGithub, tenacity, prometheus-client, opentelemetry). |
| `.github/workflows/ci.yml` | merged | Its Docker image build kept as a `docker-images` job. Its frontend job went with the frontend. Its "smoke eval" job needed a paid key in CI and was not carried over. |
| `.env.example` | merged | Provider keys and GITHUB_TOKEN listed in the root `.env.example`; B's API/frontend settings dropped with them. |
| `LICENSE` (MIT, same author) | moved to root | A had no licence; B's code is in this repository under it. |
| `README.md` | folded | Facts still true are in the new README's provenance section. |
| `tests/*` | ported or retired | `test_providers.py` → `tests/test_providers.py`; `test_regressions.py` → `tests/test_agentless.py`, `test_local_sandbox.py`, `test_permissions.py`, `test_tools.py` (search); `test_harness.py` → URL-parser tests in `tests/test_github_integration.py`, `test_no_timeout_flag` in `tests/test_bench_grading.py`. Retired with their code: `test_loop.py` (B's loop), `test_tools.py` (bash/editor output formats), `test_context.py` (tiktoken counting), `TestLocalWorkspacePaths` (`/repo` mapping), `TestSearchIndexCacheKey` (replaced by behavioural tests), `TestValidationBaseline` (replaced by `TestRegressionsByTestId`), `TestInstanceResult` (dataclass round-trip of a removed type). |

**Planning artefacts.** Of the names the brief listed, only
`docs/superpowers/specs/2026-09-01-codepilot-agent-design.md` exists in either
repository (`git ls-files | grep -iE "AUDIT|PLAN|CLAUDE|AGENTS|superpowers"`;
A's `.gitignore` lists `AUDIT.md`/`PLAN.md` as scratch, never committed).
`git grep -n ponytail` finds nothing in either tree. See D18 for the spec.

**Proposed, not done.** The hosted API still reads the caller's key from an
`X-Anthropic-Key` header; with LiteLLM it is passed to whichever provider the
configured model uses, so the name is now misleading. Renaming a public header
is a breaking change for anyone who deployed it, so it was left.

## D19. Ported from the parallel SOP-eval branch of Autonomous-SWE-Agent

Read with `git -C ../Autonomous-SWE-Agent log main..sop-eval` (read-only; no
`sop-eval-2` branch exists). Five commits:

| commit | what | here |
|---|---|---|
| `bfd20e8` strip post-base history | clone by URL, delete refs/FETCH_HEAD/reflogs, prune | already covered by D9.1 (`clone_at` fetches one commit at depth 1 into a fresh repo, so nothing after the base is ever present; the full-clone fallback fetches from a temporary clone that is then deleted) |
| `b3253c4` exact-id grading, no cap | `-rA` + per-id verdict | already covered by D9.2–3. Its third case — an *unlisted*, pre-existing failing test that `-k` selected failed a correct patch — was not yet a test here; added as `test_an_unlisted_failing_test_in_the_same_file_does_not_fail_a_correct_patch` (passes: only listed ids are judged). |
| `a5f80a6` regression gate without `-x` | | already covered by D9.4 / D12 |
| `9911b2d` distinct seed per repair sample | reproduced there: a seeded config sent seed 3 to all 4 samples | **ported.** This repository sent no seed at all, so the bug could not occur yet, but a seeded study needs seeds. `LLMClient.chat(seed=)`, `AgentLoop(seed=)`, `ArmConfig.seed`; agent attempt k → `seed*1000+k`, agentless sample k → `seed*1000+500+k`, localisation `seed*1000+999`; none sent when unseeded. Tests: `test_a_seeded_run_gives_every_sample_and_attempt_its_own_seed` (failed first: `ArmConfig` had no `seed`), `test_an_unseeded_run_sends_no_seed`. `bench.run --seed` sets it; `--no-model-seed` turns it off. |
| `2def69f` seed, api_base, Ollama | | `api_base` ported to `LLMClient` (`bench.run --api-base`). Ollama itself needs no registry entry: LiteLLM routes `ollama/<model>` strings, and the CLI's key check skips providers it does not know (D4). No Ollama run was made here. |

## D18. Planning artefact folded into docs/DESIGN.md; README rewritten

**`docs/superpowers/specs/2026-09-01-codepilot-agent-design.md`** (A's design
spec). Investigated: referenced only by A's README ("`docs/` has the design").
It held real decisions (six ADRs, the safety model, context management, the
evaluation honesty rules) and planning scaffolding (status line, milestones,
"executed as three plans"). The decisions are in `docs/DESIGN.md` §1–5, with
ADR-2 (Anthropic only) marked superseded by D2; the milestones and the
module-by-module "what survives from the old code" table are history and stay
in git (`git show 83d06d8:docs/superpowers/specs/2026-09-01-codepilot-agent-design.md`).

**README.** Rewritten. Every number in it was rechecked against the committed
files: the experiment table is recomputed from each results file's `summary`
(cost per completed task), which differs slightly from some figures in A's
README that were per-task medians (A quoted 4.19x for experiment 1; the
summaries give 4.4x; A's "1.83x" for small-file edit style was a median, the
summaries give $0.0444 vs $0.0868). Lost-code counts recomputed: 0 of 150 runs
(0 of the 40 edit-style runs). Removed claims no longer true: "Anthropic only",
"no GitHub issue → PR mode", "the container sandbox is not verified end to end"
(now verified against one official image, D10), "193 tests". Kept, verified:
11 tools (`len(REGISTRY)`), the doctor runs without a key (`tests/test_doctor.py`).

## D20. The harness check's `empty` arm now runs the tests (gap found by running it)

**Found by** the first real gold/empty check (2026-10-02, results in
`bench/results/harness_check/`). Every `empty` row read
`"no source changes to grade"` with `F2P 0/0`: `swebench.grade` returns early
on an empty diff, so the check never ran the FAIL_TO_PASS tests on the
untouched checkout. "Empty does not resolve" was therefore true by
construction and could not catch the instance it exists for — one whose
"failing" test already passes before any fix.

**Reproduced first.** `tests/test_bench_grading.py::test_the_empty_check_actually_runs_the_tests`
(a fixture instance whose FAIL_TO_PASS is a test that already passes) failed:
the check reported `ok`. `test_the_empty_check_passes_a_sound_instance` failed
with `F2P (0, 0)` where `(0, 1)` was expected.

**Fix.** `grade(..., run_if_empty=True)` applies only the test patch and runs
the graded command; `check_harness` uses it for both arms. Model arms are
unchanged: an agent that submits nothing is still unresolved without a test
run. After the fix every `empty` row shows its FAIL_TO_PASS test run and fail
(`F2P 0/1`) with every PASS_TO_PASS test passing.

The pre-fix rows are kept in `bench/results/harness_check/pre-fix/` as the
evidence; they are not a valid check of the `empty` arm.

## D21. flask-4992 needs Python < 3.12 on the local backend (environment, not harness)

**Observed.** With the local backend's default interpreter (Python 3.12.3),
the gold patch for `pallets__flask-4992` failed every graded test (`F2P 0/1,
P2P 0/18`): `werkzeug<2.3` (pinned by `bench/setups.json`, as
Autonomous-SWE-Agent's recording did) calls `ast.Str`, deprecated in 3.12, and
flask's pytest configuration turns warnings into errors.

**Not a harness bug.** The grader reported exactly what the tests did; the
check flagged it as `HARNESS PROBLEM`, which is its job. Autonomous-SWE-Agent
had hidden the same thing by appending `-W ignore::DeprecationWarning` to its
graded command; that was not reproduced here, because changing the grading
command per instance changes what the tests judge.

**Resolved by environment.** With a Python 3.11 virtualenv (`--python` pointing
at a 3.11 interpreter already on the machine) and with the official SWE-bench
image (`--backend docker --image official`, already present locally, nothing
pulled), gold resolves (`F2P 1/1, P2P 18/18`) and empty does not. The smoke
command in `bench/STUDY_PLAN.md` now says so.

## D22. Local models: provider options, and a guard against silent truncation

**What.** `LLMClient(extra={...})` sends provider options with every request
(`bench.run --model-option num_ctx=16384`); `AgentLoop`/`ArmConfig` take an
output-token cap (`--max-output-tokens`). With `num_ctx` set, a request whose
estimated prompt (x1.4) plus `max_tokens` exceeds it raises
`LLMError("context overflow …")` instead of being sent. An agentless sample
that overflows is recorded as a rejected sample; other overflows end the arm
with the error recorded, and count as a failure.

**Reproduced first, against the live Ollama server (qwen2.5:7b, Q4_K_M).**
A system prompt carrying a "secret word" plus a ~26.6k-token user message
(LiteLLM's estimate), sent with `num_ctx=16384`: no error,
`prompt_tokens=8194`, and the reply had lost the system prompt. Ollama
truncates silently, so an oversized agent or agentless prompt would have been
scored as the model failing. Tests: `tests/test_llm.py::test_a_prompt_that_cannot_fit_num_ctx_is_refused_not_truncated`
and `test_provider_options_such_as_num_ctx_are_passed_through` (both failed
first: `LLMClient` had no `extra`).

**The 1.4 margin is measured, not guessed.** For the benchmark agent's first
request on flask-4992 (system prompt + 11 tool schemas + issue), LiteLLM's
`token_counter` said 1,396 tokens and Ollama counted 1,938 (ratio 1.39).
`/api/ps` confirmed `context_length: 16384` reached the server.

## D23. Result rows carry a compact transcript (gap found by the smoke run)

**Found by** the first local-model smoke row (flask-4992, agent arm):
`stopped_by: ["finished"]`, 1 model call, no diff, and nothing in the row said
what the model had done. The row had costs and a grade but no record of the
model's words or tool calls, so a failure could not be told apart from a
harness fault.

**Reproduced first.** `tests/test_bench_e2e.py::test_a_result_says_what_the_agent_did`
failed (`InstanceResult` had no `transcript`). Now every row has `transcript`:
assistant text, each tool call (truncated arguments) and the first line of its
result, budget/error/done events; agentless rows record the localisation and
each sample's fate.

**What the flask row was.** Replayed as a single request (same system prompt,
tools, issue, temperature 0.2, seed 0): qwen2.5:7b answered with a `finish`
call whose summary *describes* a fix ("add a `mode` parameter…") without
having read or edited anything. Model behaviour, not a harness fault; the
prompt was not changed to suit it, since that would change the arm under
study. The first smoke rows, written before this field existed, were discarded
and the smoke run repeated so every committed row has a transcript.

## D24. `ollama/` models are sent to Ollama's native chat endpoint

**Found by** the first full smoke run on `ollama/qwen2.5:7b` (kept in
`bench/results/smoke/pre-fix/`). Three of the four agent runs stopped
`"ended without finish"` after 4–6 calls, and each transcript ends with the
model's reply recorded as *text*:
`{"id": "call_…", "type": "function", "function": {"name": "edit_file", "arguments": {…}}}`
— a tool call, in the nested OpenAI shape, that never became a tool call.

**Cause, from the installed library.** LiteLLM 1.103.2's `ollama/` route is
`/api/generate` (`litellm/llms/ollama/completion/transformation.py`): the whole
conversation is flattened into one prompt by `ollama_pt` (prior tool calls
included, in that nested shape), output is forced to `format: "json"`, and the
reply is treated as a tool call only if it is a top-level object with `name`
and `arguments` (lines ~266–301). A model that imitates its own history's shape
falls through to the "regular JSON" branch and comes back as content. The
`ollama_chat/` route is `/api/chat`, where Ollama applies the model's chat
template and parses tool calls itself.

**Reproduction status.** The failure was observed three times in the real run
(transcripts above). A two-turn replay did not trigger it (3/3 structured
calls on both routes), so the trigger is conversation length; it was not
reproduced on demand. The fix was therefore made on the evidence of the run
plus the library code, and the test pins the routing, not the model behaviour:
`tests/test_llm.py::test_ollama_models_use_the_native_chat_endpoint` (failed
first: the request went to `ollama/qwen2.5:7b`).

**Fix.** `LLMClient` sends any `ollama/<model>` request as
`ollama_chat/<model>` — same server, same model, same options. The smoke run
was repeated after the fix; both runs are committed.

---

# Fix phase (after the adversarial review)

An independent review judged the merge PASS-WITH-FIXES and a paid run FAIL.
The entries below (D25+) address its findings. No paid or LLM call was made
during this phase.

## D25. LiteLLM pinned, and its prices taken from the bundled map

**What.** `requirements.txt` pins `litellm==1.103.2` (was `>=1.60.0`).
`codepilot/__init__.py` sets `LITELLM_LOCAL_MODEL_COST_MAP=True` unless already
set; `tests/conftest.py` imports codepilot first so the test session uses it.

**Why, verified in the installed library.** `litellm/__init__.py:556` builds
`model_cost` from `get_model_cost_map(url=model_cost_map_url)`, which downloads
the map from GitHub unless the variable is `true`
(`litellm_core_utils/get_model_cost_map.py`). The prices a run was charged at
would then depend on the day it ran, and the request translation this code
relies on (D2's usage arithmetic, D24's routes, D30's thinking blocks) is
version-specific.

**Reproduced first.** `tests/test_pricing.py::test_importing_codepilot_pins_litellm_to_its_bundled_cost_map`
failed (`None`), then passed.

## D26. Explicit prices for the current Claude models; zero means unpriced; unpriced paid runs refused

**Reproduced first.** `claude-opus-5-5` (the current Opus, and the model the
review expected a paid run to use) had no `PRICING` row and no entry in
LiteLLM 1.103.2's map (checked: the map lists `claude-opus-5`,
`claude-sonnet-5`, `claude-haiku-4-5[-20251001]`, nothing for `*-5-5`), so
every call's cost was `None`; `Budget.record` counts such calls as
"unpriced" and never advances `spent_usd`, so the per-attempt dollar ceiling
could not trigger. `tests/test_pricing.py::test_the_current_claude_models_are_priced`
and three neighbours failed before the change.

**Prices and their sources** (`codepilot/llm.py::PRICING`, now `Price`
objects that carry their source):

| model | in $/MTok | out | cache read | cache write (5 min) | source |
|---|---:|---:|---:|---:|---|
| claude-opus-5-5 | 4.00 | 20.00 | 0.20 | 5.00 | Anthropic list price, claude-api reference (models table cached 2026-09-25); write = 1.25x input per the same reference's prompt-caching economics |
| claude-sonnet-5-5 | 2.00 | 10.00 | 0.20 | 2.50 | same |
| claude-opus-5 | 5.00 | 25.00 | 0.50 | 6.25 | same; identical to LiteLLM 1.103.2's map |
| claude-sonnet-5 | 2.00 | 10.00 | 0.20 | 2.50 | same; identical to the map |
| claude-haiku-4-5 | 1.00 | 5.00 | 0.10 | 1.25 | same; identical to the map |
| Groq rows | | | | | unchanged, marked RE-VERIFY in the code |

The 1-hour TTL (2x input) is never requested by this code, so it is not
tabulated. Cache-write prices were not independently verified beyond the
reference's 1.25x rule.

**Behaviour.** `price_for(model)`: explicit row first, then LiteLLM's map; an
input or output price that is missing **or 0** means no price
(`test_a_zero_or_missing_price_counts_as_no_price`). Local Ollama models are
priced at 0 explicitly, not by a missing price. Costs are computed from usage
by `cost_of` (uncached input, output, cache reads and writes each at their own
price), replacing `litellm.completion_cost`, whose 0.0 for unknown models the
old code then had to second-guess. `bench.run` refuses any model arm on an
unpriced model before loading anything (`test_a_paid_run_on_an_unpriced_model_is_refused_before_anything_starts`);
`bench.estimate` prints "no price: refused" instead of $0.

## D27. An append-only ledger written by every call, and spend read from it

**What.** `codepilot.llm.Ledger`: one JSON line per model call — tag, model,
token classes, cost, latency, stop reason, or the error for a failed call —
appended, flushed and fsync'd from inside `LLMClient.chat` as each call
returns or fails. The client also keeps per-tag totals in memory
(`client.spent(prefix)`). The benchmark tags every call `<instance>:<arm>`;
each result row's spend now comes from those totals, and an environment-error
row carries whatever its arms spent. `bench.run` writes the ledger beside the
results (`<out>.ledger.jsonl`, or `--ledger`).

**Reproduced first.** `tests/test_bench_e2e.py::test_agentless_spend_survives_a_later_stage_failing`:
localisation succeeds (2,000 + 50 tokens on claude-haiku-4-5), the repair stage
raises; the row reported `model_calls=0`, cost 0 — spent and not reported,
because agentless added its spend only after `run_agentless` returned. After:
1 call, $0.00225. `tests/test_llm.py::test_every_call_is_in_the_ledger_before_chat_returns`
covers the ledger itself, including a 400 recorded with cost 0.

Failed calls are recorded at $0: the providers this targets do not bill a
rejected request. That is an assumption, stated, not a measurement.

## D28. A run-wide spend cap, checked before every call

**What.** `LLMClient(max_total_usd=...)` (`bench.run --max-total-usd`). Before
every request — each retry included, compaction and both arms included,
since they share the client — the worst case of the next call is computed:
LiteLLM's prompt-token estimate x 1.5, every token priced at the dearer of
input and cache-write, plus the full `max_tokens` at the output price. If
spend so far plus that worst case exceeds the cap, `SpendCapReached` is raised
and **the run stops**: the harness re-raises it (`AbortRun`) instead of
scoring an agent failure, and `bench.run` writes an `aborted` row with the
total and exits 3. "Spend so far" includes what the ledger file already held
when the client was created, so re-running into the same ledger cannot reset
the cap. An unpriced model cannot run under a cap at all.

**Reproduced first.** There was no run-wide cap: only the agent's
per-attempt `Budget`, which agentless calls never touched.
`tests/test_bench_e2e.py::test_the_spend_cap_stops_the_whole_run_across_both_arms`
(a $0.02 cap; must stop, ledger total <= cap, every request ledgered),
`test_a_new_run_counts_what_the_ledger_already_holds` and
`test_an_unpriced_model_cannot_run_under_a_cap` failed with ImportError first.

**Limits.** The 1.5x margin on the prompt estimate is a judgement, not a
measurement against Anthropic's tokenizer (D22 measured 1.39x for Qwen).
The bound uses `max_tokens`, which the model may not use, so the cap is
conservative: the run can stop with up to one call's worst case unspent.

## D29. Sampling parameters only where they arrive; a rejected request stops the run

**Sampling, reproduced from the installed library.** LiteLLM 1.103.2's
`get_supported_openai_params` lists `temperature` for `claude-opus-5-5`, which
rejects it with a 400 (claude-api reference: Opus 4.7/4.8/5/5.5, Sonnet 5/5.5
and Fable/Mythos 5.x reject sampling parameters; Haiku 4.5 accepts them), and
does **not** list `seed` for Anthropic or Gemini — with `drop_params=True` the
seed was dropped silently while the run's own records said it was seeded.
`tests/test_llm.py::test_models_that_reject_sampling_params_are_not_sent_them`
failed first (`temperature` was in the request for claude-opus-5-5).

Now `temperature`/`seed` are sent only when `_sends(model, param)` holds (not
on the reject list, and listed by LiteLLM for that provider); whatever was
omitted is recorded on the `Reply` and in each ledger row
(`omitted_params`).

**Correction to D13.** D13 says every arm follows the same sampling schedule.
That holds where the provider honours the parameters (Groq, Ollama, OpenAI).
On Anthropic no seed exists at all, and on Opus/Sonnet 5.x temperature is not
sent either, so arms differ only by prompt and turn structure there, with the
provider's default sampling. On claude-haiku-4-5 — the model of the planned
paid run — temperature is sent and seed is not, so the 3-seed design cannot
give reproducible repeats on it; the study plan says so.

**4xx.** Any 4xx except 429 — 400, 401, 403, 404, 422, and LiteLLM's
`ContextWindowExceededError` (a 400) — now raises `ProviderRejected`, an
`AbortRun`: not retried, not scored as an agent failure, the run stops
(`test_a_rejected_request_aborts_the_run_instead_of_failing_the_agent`, which
failed first with the raw `BadRequestError`). 429 is still retried, then
reported as an infrastructure error and excluded. Our own context guard (D22)
is not a provider response and still counts as the arm failing.
