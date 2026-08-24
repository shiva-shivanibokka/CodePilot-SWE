"use client";

import InfoTip from "@/components/InfoTip";
import type { RunSummary } from "@/lib/replay";

/**
 * What this is, and what the runs on the next tab actually demonstrate.
 *
 * Written for someone who has thirty seconds and no context: what the problem
 * is, what was built, what was measured, and — the part most project pages skip
 * — what the measurements do not support.
 */
export function About({ runs = [] }: { runs?: RunSummary[] }) {
  const graded = runs.filter((r) => r.resolved !== null);
  const solved = graded.filter((r) => r.resolved);
  const spent = runs.reduce((total, r) => total + r.costUsd, 0);
  const hardest = graded.find((r) => r.difficulty?.startsWith("1-4"));

  return (
    <>
      <div>
        <p className="eyebrow">What this is</p>
        <h2>An agent that fixes real bugs, and the receipts</h2>
        <p className="lede">
          SWE-bench gives a language model a genuine GitHub issue from a real project — Django,
          sympy, Flask — and the repository as it stood the moment before the bug was fixed. The
          model has to produce a patch. It counts as resolved only when the tests written as part
          of the real fix pass, and nothing that already worked breaks. No partial credit, and no
          judgement call.
        </p>
        <p className="lede">
          This repository implements an agent that does that, twice over — a free-form loop that
          drives its own tools, and a fixed pipeline that uses none — so the two architectures can
          be run on identical inputs and compared. Everything is built from raw API calls rather
          than a framework, because the parts a framework hides are the parts worth showing.
        </p>
      </div>

      <div className="rule">
        <span>@@ what the demo shows @@</span>
        <b>
          {solved.length} of {graded.length} resolved
        </b>
        <span>for ${spent.toFixed(2)} in total</span>
      </div>

      <div className="cols">
        <div>
          <h3>
            It is a recording, and that is deliberate
            <InfoTip
              label="why it is a recording"
              text="A live run needs several minutes, a checkout of a third-party repository, and a shell to execute code nobody has vetted. No free host offers that, and a public button that runs arbitrary repositories would be a bad idea even with the budget for it."
            />
          </h3>
          <p className="lede">
            Each run on the next tab happened once, for real, and was committed. The model&apos;s
            reasoning, the tool calls it chose, the tokens and dollars the provider billed, the
            patch it wrote and the test output it was graded on are all exactly what occurred. Only
            the pacing is altered — the pauses where the model was thinking are clamped so a run of
            several minutes is watchable in half of one.
          </p>
        </div>

        <div>
          <h3>
            The failure is the point
            <InfoTip
              label="why a failed run is published"
              text="It was not re-run to get a better result. An agent that solves easy issues and visibly breaks on hard ones tells you where the boundary is; four staged successes tell you nothing you can rely on."
            />
          </h3>
          <p className="lede">
            {hardest ? (
              <>
                The instance the benchmark&apos;s annotators rated hardest —{" "}
                <a className="link" href={`#run/${hardest.id}`}>
                  {hardest.id}
                </a>{" "}
                — was not resolved, and it is published with its diff and its test output.
              </>
            ) : (
              <>The hardest instance attempted is published with its diff and its test output.</>
            )}{" "}
            The issue describes one bug; the fix the maintainers shipped solved a larger problem
            the issue never mentions. The agent solved what it was asked. The benchmark grades what
            was actually needed. That gap is what &quot;hard&quot; means here.
          </p>
        </div>
      </div>

      <div className="rule">
        <span>@@ what it does not show @@</span>
        <b>read this before quoting a number</b>
      </div>

      <dl className="facts">
        <div className="fact">
          <dt>
            Not a resolve rate
            <InfoTip
              label="why this is not a resolve rate"
              text="Four instances is not a score over 300, and they were chosen for having environments that still build on a modern interpreter — which is its own selection bias. The benchmark table is deliberately empty rather than extrapolated."
            />
          </dt>
          <dd className="small">{graded.length} of 300</dd>
        </div>
        <div className="fact">
          <dt>
            Grading is a proxy
            <InfoTip
              label="how grading works here"
              text="The instance's test patch is applied after the agent finishes, then its FAIL_TO_PASS and PASS_TO_PASS tests run, capped at twenty. The official grader re-runs the full sets inside per-instance images. Close, not identical."
            />
          </dt>
          <dd className="small">Capped at 20 tests</dd>
        </div>
        <div className="fact">
          <dt>
            One model
            <InfoTip
              label="model coverage"
              text="Every run here used claude-sonnet-5. The client is provider-agnostic and the same loop runs on OpenAI, Google or Groq with your own key, but no cross-provider comparison has been recorded."
            />
          </dt>
          <dd className="small">Sonnet 5 only</dd>
        </div>
        <div className="fact">
          <dt>
            Agentless unrecorded
            <InfoTip
              label="the agentless arm"
              text="The three-phase pipeline runs and its parts are unit-tested, but no end-to-end recording of it is published, so the head-to-head comparison this project is built for has not actually been performed yet."
            />
          </dt>
          <dd className="small">Not yet run</dd>
        </div>
      </dl>

      <div className="rule">
        <span>@@ the hard parts @@</span>
        <b>what this was actually built to demonstrate</b>
      </div>

      <ol className="steps">
        <li>
          <strong>An interface the model can use without tripping over it</strong>
          <p>
            The editor refuses an edit whose target string is not unique, which is what stops the
            classic overwrite-the-wrong-function failure. Output is truncated with an explanation
            of how to page through it. The tool descriptions are the engineering.
          </p>
        </li>
        <li>
          <strong>A trust boundary that is a seam, not a dependency</strong>
          <p>
            The model runs arbitrary shell commands against arbitrary third-party code. That is
            confined to a throwaway container — or, on a machine without Docker, a temp directory
            with its own virtualenv. Same loop, same tools, and the model cannot tell which it has.
          </p>
        </li>
        <li>
          <strong>Accounting that cannot quietly drift</strong>
          <p>
            Cost is read back from the provider rather than estimated from a price table, the
            grading command is recorded alongside its output, and a build-time check fails the site
            if a recording is missing, malformed, or carries anything key-shaped.
          </p>
        </li>
      </ol>
    </>
  );
}
