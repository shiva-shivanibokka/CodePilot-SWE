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

## D30. At most two requests per call, one retry layer

`LLMClient` retried up to 6 times (7 requests per call, each billable if the
provider served it and the response was lost), and LiteLLM's
openai-compatible routes (Groq, OpenAI) passed requests through the OpenAI
SDK, whose own default is 2 retries — two stacked layers. Now `max_retries=1`
in the client and `num_retries=0, max_retries=0` on every request, so one call
is at most two requests. Reproduced first:
`tests/test_llm.py::test_a_persistent_rate_limit_costs_at_most_two_requests`
ran out of scripted errors (the 6th retry), then passes with exactly 2.
Every attempt passes the spend cap (D28) and is ledgered (D27).

## D31. Claude thinking blocks survive the round trip

Claude 5-family models think by default (Opus 5.5 cannot turn it off) and
return `thinking` blocks — empty text by default, with a signature — ahead
of their `tool_use` blocks; the API requires them back unchanged on the
next request of the turn (claude-api reference, thinking & effort section).
LiteLLM 1.103.2 exposes them as `message.thinking_blocks` and accepts them on
an assistant message under the same key (`transformation.py` ~L2531-2689).

**Reproduced first.** `reply_from_response` ignored `thinking_blocks`, so
they never reached the conversation, and `to_openai_messages` dropped any
thinking block by design (D2's note). `tests/test_llm.py::test_thinking_blocks_round_trip_with_tool_calls_and_cache_usage`
failed (`['text', 'tool_use']`).

**Fix.** Thinking and redacted-thinking blocks are kept, first and verbatim,
in `Reply.content`; `to_openai_messages` sends them back as `thinking_blocks`;
`_request` strips them for any provider other than Anthropic (they are bound
to the model that produced them). The test drives a fake Claude response with
thinking blocks, a tool call and cache usage fields, then a 429 (retried once)
and a 400 (aborts the run). `test_litellm_puts_echoed_thinking_ahead_of_the_tool_call_in_the_anthropic_body`
runs LiteLLM's own Anthropic transform offline on the result: the assistant
turn is `thinking -> tool_use`, signature intact.

**Not covered.** No live Claude request was made, so preserved-thinking's
history-editing check (compaction rewrites history; D32 counts it) is
untested against the real API. Compaction on a Claude 5.5 model may make
later thinking blocks invalid; the planned paid run uses claude-haiku-4-5,
which does not think unless asked, so it does not hit this.

## D32. Compaction calls are budgeted and reported

`Conversation.compact` (`codepilot/context.py`, the summary call) makes a
model call of its own; `AgentLoop` neither recorded it in the `Budget` nor
emitted a `COST` event, so every compaction was spend the per-attempt budget,
the harness's `Spend` and the result rows never saw. Reproduced first:
`tests/test_loop.py::test_compaction_calls_are_counted_in_the_budget_and_the_cost_events`
counted 10 COST events for 13 model calls. Now `compact` leaves its reply on
`last_compaction_reply`, and the loop records it in the budget and emits a
`COST` event with `purpose="compaction"`. In the benchmark the spend also
reaches the row and the ledger through the client (D27), and the run-wide cap
(D28) checks it before it is made.

## D33. A response cache, so a rerun does not pay twice

`LLMClient(response_cache=dir)` (`bench.run --response-cache`). The key is a
SHA-256 of the full request (model, messages, tools, max_tokens, sampling
parameters actually sent, provider options) plus a caller tag; the agent tags
each attempt (`<instance>:<arm>:attempt-<k>`) and agentless each localisation
and sample, so two attempts that send byte-identical requests — which happens
on providers that take no seed — stay two samples instead of one replayed
twice. A hit costs nothing: it is ledgered with `cached: true` and `$0`, and
keeps the original usage so token measurements still hold. Entries are
written atomically (temp file, then rename). Test:
`tests/test_llm.py::test_a_rerun_is_served_from_the_response_cache_and_not_paid_twice`
(failed first: no such option). The default `--out`, its ledger and
`bench/.cache/` are gitignored.

Caveat: a cached reply replays one sample. Use a fresh cache directory for an
independent repeat.

## D34. A rolling cache breakpoint on the latest message, and a prefix-size check

**Reproduced first.** Only the system prompt carried `cache_control`. The
minimum cacheable prefix on claude-haiku-4-5 is **4,096 tokens** (claude-api
reference, prompt-caching API table: Opus 4.5/4.6 and Haiku 4.5 at 4,096;
Opus 5/5.5, Sonnet 5.5 and Fable at 512; Sonnet 5 and 4.x at 1,024), and the
agent arm's system prompt plus 11 tool schemas is ~1,233 tokens by LiteLLM's
generic counter (Ollama counted 1,658-1,938 for the same text with an issue
attached, D22). So on Haiku nothing was ever cached and every turn paid the
whole growing history at the full input price.
`tests/test_llm.py::test_the_latest_message_carries_a_cache_breakpoint_on_anthropic`
failed first.

**Fix.** For Anthropic only, `mark_latest_for_cache` puts a breakpoint on the
last block of the latest message of every request — both arms, since it lives
in the client. Each request writes the conversation so far; the next one
reads it back once it passes the model's minimum. Two breakpoints per
request (system + latest), of the four allowed. Checked through LiteLLM's own
Anthropic transform offline: the marker lands on the tool-result's text block
and on a user text block.

**Prefix check.** `codepilot.llm.cache_minimum(model)` and
`codepilot.bench.harness.cache_prefix_report(model)`; `bench.run` prints the
report and writes it into a `config` row at the top of the results file,
with every cap. Result for the planned model:

| prefix (LiteLLM estimate) | tokens | caches on its own on claude-haiku-4-5 (min 4,096)? | on claude-opus-5-5 (min 512)? |
|---|---:|---|---|
| agent: system + tools | ~1,233 | no | yes |
| agentless localise: system | ~279 | no | no |
| agentless repair: system | ~313 | no | no |

So on Haiku caching comes only from the rolling breakpoint, for an agent
conversation once it passes ~4k tokens, and for an agentless repair prompt
(issue + whole file) when that alone passes 4k, which lets samples 2..N of the
same file read it back. That asymmetry follows from the prompts' shapes, not
from a different caching rule, and is stated in the study plan.
`test_the_prefix_report_says_haiku_cannot_cache_the_system_prompt_alone`.

## D35. Contamination between arms is detected; arm order is randomised

**Reproduced first.** Both arms run in one environment, restored to the
baseline between them (D14). `restore` resets tracked files and removes new
ones, but keeps the ignored files setup created as they are, and never
touches installed packages. An arm that rewrote a kept file (or pip-installed
something) left the next arm — and its own grading — in a changed
environment, silently. `tests/test_bench_e2e.py::test_an_arm_that_changes_the_kept_environment_is_flagged`
(the first arm runs `echo tampered > build/artifact.txt` on a file setup
created) failed first: no such field existed, and nothing noticed.

**Fix (detection, not prevention).** `BenchEnv.fingerprint()` records the
content hash of every kept ignored file and (path, size, mtime) of every file
in the task interpreter's site-packages (run inside the sandbox, so it is the
venv's or the container's). The harness fingerprints once after setup and
again after each arm's grading; any difference is listed in the row's
`contamination` field with a note. The review's stronger option — a fresh
environment per arm — was not taken: it doubles setup time and clone traffic
per instance, and detection makes any contamination visible and excludable.
`test_the_fingerprint_really_sees_the_installed_packages` checks that it sees
hundreds of real site-packages entries and is stable when nothing changed.

**Arm order.** Shuffled per instance with `random.Random(f"{seed}:{instance_id}")`,
recorded in each row (`arm_order`), so neither arm systematically runs first
on a freshly built environment. `test_arm_order_is_randomised_per_instance_and_recorded`.

## D36. The shared prompt no longer claims "no network access"

`SHARED_BASE` told every arm the checkout had "no network access". On the
Docker backend that is true (the container is taken off every network before
the first command, D10); on the local backend it is false — commands run on
the host. A prompt that lies about the environment on one backend is a
confound between backends. Reproduced as
`tests/test_bench_e2e.py::test_the_shared_prompt_makes_no_claim_the_local_backend_breaks`.
The sentence is replaced by an instruction that holds on both ("Do not try to
download anything: work with what is installed"). The paid study uses the
Docker backend with official images, where the absence of a network is
enforced rather than asserted (STUDY_PLAN.md). Changing `SHARED_BASE` changes
every arm's prompt identically; the smoke rows were produced with the old
sentence.

## D37. Ported from the SOP-eval worktree: the frozen dataset and an official second grader

Source, credited: the uncommitted `eval_sop/` directory in the worktree another
agent used to evaluate Autonomous-SWE-Agent
(`…/scratchpad/wt/Autonomous-SWE-Agent`, branch head `9911b2d`), read on
2026-10-04 and **copied, not moved** — that worktree was not modified.

1. **Frozen SWE-bench Lite.** `eval_sop/data/swebench_lite.json.gz` (300
   rows, HF revision `b0dde10…`, fetched 2026-10-01; gz sha256 `b4926541…`,
   uncompressed sha256 `7d87279f…` checked on every load) and
   `eval_sop/instances.py` → `codepilot/bench/data/` and
   `codepilot/bench/instances.py` (plus a `sample(k, seed)` helper).
   `swebench.load_swebench_lite` / `load_instances` now read it by default
   (`live=True` for the old HTTP path) and `bench.run --sample K --seed S`
   draws `instances.seeded_order(S)[:K]`. **Changed behaviour:** the earlier
   `random.Random(seed).sample(sorted_rows, K)` drew a different set for the
   same seed; nothing was ever run with it. Tests:
   `tests/test_bench_instances.py` (300 unique rows, offline loading with
   `urlopen` patched to fail, same seed → same instances, a tampered file is
   refused).
2. **Official grader**, optional. `eval_sop/grader.py` →
   `codepilot/bench/official_grader.py`, `eval_sop/swebench_compat.py` →
   `codepilot/bench/swebench_compat.py` (verbatim: stubs `datasets`/`modal`
   so the `swebench` package's pure functions import without them).
   `swebench` is imported only when grading and is not a requirement. Two
   changes: the **"already-applied" fallback is removed** — the original, when
   every forward apply failed, ran `git apply --check --reverse` and recorded
   the patch as applied if that succeeded (from reading `grade_patch`), which
   grades code the agent did not write; and **images are not pulled unless
   `allow_pull=True`** (several GB each). Tests:
   `test_the_official_grader_does_not_count_a_reverse_applying_patch_as_applied`,
   `test_the_official_grader_will_not_pull_an_image_unless_asked`. Not run
   end to end here (the `swebench` package is not installed).
3. **Noted as future work, not ported:** the worktree's agentless gate
   (`eval_sop/agentless_arm.py`) measures regressions with each repository's
   *official* test command and log parser, because Django's runner never
   prints "N passed", so a count-based gate rejected every Django candidate.
   This repository's selection parses per-test outcomes but runs pytest, and
   skips the regression gate entirely for `django/django` (D12); using the
   official per-repo command there is listed in STUDY_PLAN.md.

## D38. Pending rows, status-200 errors, a fixed ledger, generous token estimates

Four gaps found by another project's second review, checked here and applied.

1. **Pending-row ledger.** A request that dies after it was sent — an
   `overloaded_error` delivered inside a status-200 stream, an
   `httpx.ReadTimeout` or `RemoteProtocolError` while the body arrives, a
   `KeyboardInterrupt`, a cancelled task — may be billed, and D27's ledger
   recorded it at $0. Now each request is appended **before it is sent** as
   `status: "pending"` at its worst-case cost (D28's bound), and settled by a
   second row with the same `call_id` and the real cost once a response is
   parsed. Anything else leaves it charged at the worst case, in the file and
   in the client's running total that the cap reads. A provider's clean 4xx
   is settled at $0. The file stays append-only: "update" is a later row that
   supersedes, and `Ledger.total_usd` resolves them. Rows also carry
   `unsettled_worst_usd`. Reproduced first —
   `tests/test_llm.py::test_a_call_that_dies_mid_response_stays_charged_at_the_worst_case`
   (ReadTimeout, RemoteProtocolError), `test_an_interrupted_call_stays_charged`
   (KeyboardInterrupt, CancelledError), `test_a_success_settles_the_pending_row_to_the_real_cost`,
   `test_a_genuine_4xx_settles_at_zero` — six failed before the change. The
   fakes raise from inside the request (our client does not stream, so
   "mid-response" means from within `acompletion`, after the request left).
2. **Status-200 errors.** An `overloaded_error` / `api_error` that arrives
   with status 200 is retryable (counts against D30's limit), stays charged
   at the worst case, and is never treated as a $0 4xx.
   `test_an_overloaded_error_in_a_200_is_retried_and_both_attempts_are_charged`:
   two requests, two pending rows, both charged.
3. **Fixed ledger path.** The ledger defaulted to `<out>.ledger.jsonl`, so a
   new `--out` started an empty ledger and reset the run-wide cap. Now
   `bench.run` always uses `bench/spend-ledger.jsonl` (`PROJECT_LEDGER`); the
   `--ledger` option is gone. `tests/test_bench_instances.py::test_the_spend_ledger_does_not_move_with_the_output_file`.
4. **Token estimate.** The worst case now uses
   `max(LiteLLM's count x 1.5, chars / 2.5, UTF-8 bytes / 3)` over the request's
   messages and tools (`estimate_prompt_tokens`), so non-Latin text is not
   undercounted. `test_the_token_estimate_does_not_undercount_non_latin_text`.

Also fixed in the tests' fake: it raised only `Exception` subclasses, so a
scripted `KeyboardInterrupt` was returned as a "response"; it now raises any
`BaseException`.

## D39. A dry run that prices the design at its caps, and a per-request prompt bound

**Prompt bound.** A design's worst case cannot be computed if a single request
can be arbitrarily large (an agentless repair prompt carries a whole file).
`LLMClient(max_prompt_tokens=...)` (`bench.run --max-prompt-tokens`, default
50,000) refuses any request whose generous estimate (D38) exceeds it, before it
is sent; the refusal is an arm failure, like D22's local overflow, not a run
abort. `tests/test_pricing.py::test_a_prompt_over_the_bound_is_refused_not_sent`.

**Dry run.** `bench.run ... --dry-run` prints the worst case of the planned
design from its caps and exits 2 if that, plus what the project ledger already
holds, exceeds `--max-total-usd` — no keys loaded, nothing run.
`codepilot.bench.estimate.worst_case_design`:

* agent, per attempt: the per-attempt dollar budget (checked before each call,
  compaction included, D32) plus one call of overshoot
  (`max_prompt_tokens` at the dearer of input and cache-write + the agent's
  output cap);
* agentless, per instance: localisation (2,048 output tokens) and, per sample,
  one repair call (4,096) and its possible re-ask (8,192), each with a full
  `max_prompt_tokens` prompt.

Tests: `test_the_worst_case_of_a_design_is_computed_from_the_caps`,
`test_a_dry_run_refuses_a_design_whose_worst_case_exceeds_the_cap` (both failed
first). For the planned design — 20 instances, 1 seed, both arms, N = 1,
claude-haiku-4-5-20251001, $0.40 per agent attempt, 50,000-token prompt bound,
2,048 output tokens per agent call:

```
worst case agent     $9.45
worst case agentless $5.18
worst case total     $14.64  (+ $0.00 already in spend-ledger.jsonl)
```

## D40. Correction to D39: an agent step can overshoot its budget by two calls

Re-reading `AgentLoop.run`: `budget.check()` runs once per step
(`codepilot/agent/loop.py:91`), and a step can then make **two** calls — the
compaction summary (D32) and the model call. D39's worst case allowed one. The
agent term is now `max_cost_per_attempt + 2 x per-call worst case` (the
compaction summary's output cap is 2,048). The updated test expectation
failed against D39's formula, then passed. The planned design's dry run is now:

```
worst case agent     $10.91
worst case agentless $5.18
worst case total     $16.09
```

D39's $14.64 is superseded.

## D41. Round-3 spend controls: one process, atomic reservation, a user-level ledger, a hard maximum

Holes found in another project's third review round, checked against this
code and closed. Tests: `tests/test_spend_controls.py`.

1. **Atomic check-and-reserve.** D28 checked the cap from in-memory totals and
   D38 then appended a pending row — two steps, so two writers could both see
   room for one more call. The ledger is now SQLite: `Ledger.reserve` reads
   the total, checks the cap and inserts the pending row inside one
   `BEGIN IMMEDIATE` transaction; settlement is an `UPDATE`.
   `test_two_clients_cannot_both_reserve_the_last_of_the_cap`: two clients,
   one ledger, a cap with room for 1.5 worst cases, called concurrently —
   exactly one is refused and exactly one request is sent. (Under D28/D38
   each client checked only its own memory plus the ledger as it was at
   construction, so both would have gone.)
2. **User-level ledger.** `%LOCALAPPDATA%\sop_eval\codepilot_swe\ledger.sqlite`
   (`~/.local/share/...` elsewhere), outside the repository: another checkout
   or output directory cannot reset spend. No environment override; tests
   redirect `codepilot.llm.LEDGER_DIR` with an autouse fixture
   (`tests/conftest.py`). Supersedes D38's in-repo `bench/spend-ledger.jsonl`.
3. **Process lock.** `codepilot/bench/runlock.py`: an `O_EXCL` lock file beside
   the ledger, held for the life of any paid run, released in a `finally`
   (normal exit, exception, Ctrl-C). A second paid run is refused with the
   holder's PID. `--break-stale-lock` removes a lock only if its PID is not
   running — checked with `tasklist` on Windows (where `os.kill` would
   terminate the process) and `os.kill(pid, 0)` on POSIX; nothing is ever
   killed. Tests: `test_a_second_paid_run_is_refused_and_a_stale_lock_can_be_recovered`,
   `test_the_lock_is_released_on_ctrl_c`, `test_a_running_pid_is_seen_as_running`.
4. **Hard maximum.** `bench.run.PROJECT_MAX_USD = 20.0`, the planned cap
   (STUDY_PLAN.md; this repository's share of the $50 total is $25, kept 20%
   under). A paid run must give `--max-total-usd` and cannot give more.
   `test_paid_runs_need_a_cap_no_higher_than_the_project_maximum`.
5. **Transport errors, checked in the installed code.** LiteLLM 1.103.2 sends
   requests with `httpx` (0.28.1): it converts `httpx.TimeoutException` to
   `litellm.Timeout` (status 408), and on `RemoteProtocolError`/`ConnectError`
   **retries once itself on a fresh connection** and lets a second failure
   through raw (`llms/custom_httpx/http_handler.py:817-845`). `httpx2`
   (2.13.1, used by the Anthropic SDK 1.x) is also installed. Now any
   `httpx`/`httpx2` `TransportError`, `litellm.Timeout` and
   `litellm.APIConnectionError` is retried once (D30's limit) with every
   attempt charged at its worst case, and 408/429 are never treated as clean
   4xx rejections — the first version of this change scored `litellm.Timeout`
   (408) as a run-ending 4xx, caught by the parametrised test.
   **Known gap:** LiteLLM's own reconnect-retry happens inside one of our
   requests; if the first attempt was billed and the retry succeeds, the
   ledger records one call. At most one extra call per occurrence, absorbed by
   the 20% margin; not closable without patching LiteLLM.
6. **Model recorded and checked.** Every settled row records the model the
   API reported (`reported_model`). A reply from a different model (after
   removing provider prefix and date suffix) is settled and then aborts the
   run (`ModelMismatch`). Tests: `test_an_answer_from_another_model_is_recorded_and_aborts_the_run`,
   `test_a_dated_answer_to_an_alias_is_not_a_mismatch`.
7. **No unledgered client.** `LLMClient` without a ledger now uses the
   user-level one, so the CLI, the web UI, `solve.py` and the harness's own
   fallback client are all ledgered. `test_a_client_built_without_a_ledger_still_has_one`
   and a grep test that nothing in the package passes `ledger=None`.

Also: the Docker backend no longer starts from an image that is not already
present (`containers.run` would pull it silently, several GB); the operator
pulls with `docker pull`, which shows the size.
`test_a_missing_docker_image_is_never_pulled_silently`.

## D42. Hygiene from the review

* **D15's test list was incomplete.** The complete fate of every test in
  Autonomous-SWE-Agent's suite (`git show 9fd0895:tests/<file>`):
  - `test_providers.py`: `TestRegistry`, `TestLitellmModel`,
    `TestProvidersPayload`, `TestToOpenAITools` → `tests/test_providers.py`;
    `TestAssistantMessage` → covered by
    `tests/test_llm.py::test_tool_use_and_tool_result_blocks_become_openai_tool_messages`;
    `TestLLMConfig` retired with `LLMConfig`.
  - `test_harness.py`: `TestGithubUrlParser` → `tests/test_github_integration.py`;
    `TestBuildTestCommand::test_no_timeout_flag` → `tests/test_bench_grading.py`;
    the other four `TestBuildTestCommand` tests asserted the replaced command
    shape (`-x`, capped node ids, whole-suite fallback) and are superseded by
    `test_the_graded_command_has_no_exitfirst_no_k_and_no_cap`,
    `test_bare_names_run_the_patched_files`,
    `test_django_runs_its_own_runner_on_the_patched_modules`;
    `TestInstanceResult` retired with B's `InstanceResult`.
  - `test_regressions.py`: `TestJsonExtraction`, `TestSearchReplacePatching` →
    `tests/test_agentless.py`; `TestLocalWorkspaceRefusals` →
    `tests/test_permissions.py`; `TestLocalWorkspacePaths::test_provider_keys_are_stripped…`
    → `tests/test_local_sandbox.py`, the other five (`/repo` path mapping)
    retired with the mapping; `TestPytestSummaryParsing` and
    `TestValidationBaseline` (count-based) superseded by per-test parsing
    (`tests/test_bench_testlog.py`) and `TestRegressionsByTestId`;
    `TestSearchIndexCacheKey` superseded by the behavioural tests in
    `tests/test_tools.py`; `TestBashTimeoutReporting` retired with B's bash
    tool (CodePilot's sandbox appends `[timed out after Ns]`,
    `tests/test_local_sandbox.py::test_a_timeout_kills_the_whole_process_tree`);
    `TestTruncatedSampleRetry`: its two `apply_search_replace` checks were
    ported, but **the re-ask itself had no test** —
    restored now as `tests/test_agentless.py::test_a_truncated_sample_is_asked_again_with_twice_the_room`
    (passes: the behaviour was intact, only its test was lost).
  - `test_loop.py`, `test_tools.py`, `test_context.py`: retired with B's loop,
    bash/editor tools and tiktoken context manager (D15 rows).
* **D19 said "Five commits".** `git log main..sop-eval` lists six; D19's table
  omitted `dd0b757` (an import-order fix for ruff in `record_run.py`, which was
  not ported because `record_run.py` was retired, D15).
* **CI** no longer lists the nonexistent `rebuild` branch.
* **An empty, untracked `evals/` directory** left by the D16 move was removed.
* **Personal paths in results.** 28 occurrences of the author's home directory
  (temp checkouts, the Anaconda interpreter in a traceback) in the carried-over
  recordings and the harness-check/smoke rows were replaced by `<HOME>`; all
  files still parse. `bench.run`'s writer now redacts the home directory in
  every new row as well as key-shaped strings.
  `tests/test_bench_instances.py::test_committed_results_carry_no_personal_paths`
  and `test_new_result_rows_have_the_home_directory_redacted` (both failed
  first).

## D43. The funded study, and its runbook under test

The fix-phase brief funds one study: this repository's $25 share of a $50 key,
spent on `claude-haiku-4-5-20251001`. `bench/STUDY_PLAN.md` now has a "Funded
study" section ahead of the (unfunded) full design: 20 instances from
`instances.sample(20, seed=0)`, 1 seed, both arms at N = 1, Docker with the
official images, $0.40 / 40 calls per agent attempt, 50,000 prompt tokens and
2,048 output tokens per call, and a $20 cap equal to `PROJECT_MAX_USD` (D41),
a margin of 20% under the $25 share. Numbers, re-run for this entry:
expected $1.54 (x3: $4.61) from `codepilot.bench.estimate --results
bench/results/smoke/2026-10-02-qwen2.5-7b.jsonl --instances 20 --seeds 1
--attempts 1`; worst case $16.09 from the documented `--dry-run`. Order:
operator-pulled images, the free gold/empty check (failures excluded), both
dry runs, a 1-instance canary at `--max-total-usd 1`, then the main run, whose
$20 cap includes the canary (one user-level ledger). Caveats listed there: no
seed on Claude, `/testbed` shadowing for Django and compiled repos, the Django
regression gate (D12, D37) as future work, LiteLLM's reconnect-retry gap
(D41), thinking not exercised live (D31).

Round-3 item 8: the commands are in fenced blocks after `<!-- runbook:NAME -->`
markers, and `tests/test_runbook.py` parses them and runs each through
`bench.run.main` with environments and grading faked but the real client,
ledger and lock (only `litellm.acompletion` is faked). It checks the harness
check makes no model call and takes no lock; both dry runs exit 0; the canary
and main run exit 0, hold the lock while the model runs and release it, write
40 ledgered requests (the canary instance replayed from the response cache)
and a `config` row with the documented cap; and the main run aborts (exit 3)
with no request sent when the ledger is already at the cap. All five failed
before the section existed (no runbook blocks). The "Not in this plan" note on
the official grader was corrected: it is wired in (D37) but not run.

## D44. README: estimate source, spend controls, funded run

* The README said the study's cost estimate "is built on" the carried-over
  Autonomous-SWE-Agent recordings; since the smoke run it is built on this
  repository's own smoke tokens, with the recordings kept for comparison
  (STUDY_PLAN.md, "Cost estimate"). Corrected.
* The two paid examples in "The benchmark" would now be refused without
  `--max-total-usd` (D41); the cap was added to them. A "Spend controls"
  paragraph summarises D25–D41.
* "SWE-bench comparison" names the funded Haiku run and its numbers (D43).
* Test count updated to the current suite: 387 passed, 1 skipped.
