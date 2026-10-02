import InfoTip from "@/components/InfoTip";

export function Architecture() {
  return (
    <>
      <div>
        <p className="eyebrow">How it works</p>
        <h2>Same issue, two architectures</h2>
        <p className="lede">
          Both approaches share exactly two things — a workspace and an LLM client — and differ in
          everything else. That is what makes them comparable: identical inputs, identical
          isolation, identical cost accounting, and no framework in between hiding which one did
          what.
        </p>
      </div>

      <div className="rule">
        <span>@@ the two paths @@</span>
        <b>agentic vs. agentless</b>
      </div>

      <div className="cols">
        <div>
          <h3>
            Agentic — a tool-use loop
            <InfoTip
              label="the agentic approach"
              text="One model drives its own loop with three tools until it decides it is finished. More capable on hard issues, and more expensive: every turn re-sends the whole conversation."
            />
          </h3>
          <ol className="steps">
            <li>
              <strong>Search</strong>
              <p>
                BM25 over the repository, fused with sentence embeddings when they are installed,
                finds candidate files before anything is read. Reading first is the expensive
                mistake — it fills the context window with noise.
              </p>
            </li>
            <li>
              <strong>Reproduce</strong>
              <p>
                The agent writes a script that triggers the bug and runs it, confirming it
                understands the problem before changing anything.
              </p>
            </li>
            <li>
              <strong>Edit</strong>
              <p>
                <code className="mono">str_replace</code> requires the target string to match
                exactly once. If it is ambiguous the edit is refused, which is what stops the
                classic overwrite-the-wrong-function failure.
              </p>
            </li>
            <li>
              <strong>Verify, then stop</strong>
              <p>
                It runs the suite and emits <code className="mono">&lt;DONE&gt;</code> when
                satisfied. A context-budget manager compresses old turns as the window fills.
              </p>
            </li>
          </ol>
        </div>

        <div>
          <h3>
            Agentless — a fixed pipeline
            <InfoTip
              label="the agentless approach"
              text="No tools at all. Three deterministic phases, with selection decided by running tests rather than by the model's own confidence — cheaper, and reproducible in a way a free-form loop is not."
            />
          </h3>
          <ol className="steps">
            <li>
              <strong>Localize</strong>
              <p>
                A repository map — file tree plus class and function signatures — is enough for the
                model to name suspect files and functions without touching a tool.
              </p>
            </li>
            <li>
              <strong>Repair ×N</strong>
              <p>
                Candidates are sampled at temperature as search/replace blocks: a few dozen tokens
                whatever the file&apos;s size, so a large file cannot truncate the patch away.
              </p>
            </li>
            <li>
              <strong>Validate</strong>
              <p>
                Each candidate is applied and tested in the workspace. The winner is chosen by pass
                rate, not by the model — the one place where the pipeline is strictly more
                trustworthy than the agent.
              </p>
            </li>
            <li>
              <strong>Submit the best</strong>
              <p>
                The highest-scoring patch is kept and everything else discarded, with every
                rejection recorded and its reason kept.
              </p>
            </li>
          </ol>
        </div>
      </div>

      <div className="rule">
        <span>@@ the trust boundary @@</span>
        <b>why the workspace is an interface</b>
      </div>

      <p className="lede">
        The model issues arbitrary shell commands against arbitrary third-party code, so execution
        is confined and one workspace per task guarantees no state bleeds between benchmark
        instances. Making that an interface rather than a class is what lets the project run on a
        machine with no Docker at all — same loop, same tools, different backend, and the model
        cannot tell which one it has.
      </p>

      <dl className="facts" style={{ marginTop: 22 }}>
        <div className="fact">
          <dt>
            Docker backend
            <InfoTip
              label="the Docker backend"
              text="A fresh container per task from a pinned image: network_mode=none, non-root, memory and CPU capped, host filesystem never mounted — files move in and out as tar streams, the same way the official SWE-bench harness does it."
            />
          </dt>
          <dd className="small">Isolated</dd>
        </div>
        <div className="fact">
          <dt>
            Local backend
            <InfoTip
              label="the local backend"
              text="A temp directory with its own virtualenv. No isolation: commands run as you, on your machine. It exists so the project is runnable without Docker, ships with a refusal list and a hard timeout on every command, and is never the right choice for code you do not already trust."
            />
          </dt>
          <dd className="small">No Docker</dd>
        </div>
        <div className="fact">
          <dt>
            Providers
            <InfoTip
              label="provider support"
              text="One normalized complete() contract over LiteLLM, so the same loop runs on Anthropic, OpenAI, Google or Groq with the caller's own key. Nothing is read from the server environment and nothing is stored."
            />
          </dt>
          <dd>4</dd>
        </div>
        <div className="fact">
          <dt>
            Observability
            <InfoTip
              label="observability"
              text="OpenTelemetry spans answer 'what did this run actually do'; Prometheus counters and histograms answer 'resolve rate, cost, latency' across many runs. Both are wired through the same two code paths."
            />
          </dt>
          <dd className="small">OTel + Prom</dd>
        </div>
      </dl>
    </>
  );
}
