import InfoTip from "@/components/InfoTip";

const REPO = "https://github.com/shiva-shivanibokka/Autonomous-SWE-Agent";

export function RunItYourself() {
  return (
    <>
      <div>
        <p className="eyebrow">Locally, with your own key</p>
        <h2>Run it yourself</h2>
        <p className="lede">
          The hosted page is a replay because a live run needs several minutes, a checkout of a
          third-party repository, and a shell to execute code nobody has vetted. None of that
          belongs behind a public button — but all of it works on your machine in two commands.
        </p>
      </div>

      <div className="rule">
        <span>@@ without docker @@</span>
        <b>the fast path</b>
        <span>a temp checkout with its own virtualenv</span>
      </div>

      <pre className="shell-block">
        <span className="c"># clone, install, add one provider key</span>
        {"\n"}
        <span className="p">$</span> git clone {REPO}
        {"\n"}
        <span className="p">$</span> cd Autonomous-SWE-Agent && pip install -e &quot;.[dev]&quot;
        {"\n"}
        <span className="p">$</span> cp .env.example .env{"   "}
        <span className="c"># set ANTHROPIC_API_KEY (or OPENAI / GEMINI / GROQ)</span>
        {"\n\n"}
        <span className="c"># run the same instance this page replays, graded the same way</span>
        {"\n"}
        <span className="p">$</span> python -m eval.record_run \{"\n"}
        {"    "}--instance pallets__flask-4992 --backend local \{"\n"}
        {"    "}--setup &apos;python -m pip install -q -r requirements/tests.txt&apos; \{"\n"}
        {"    "}--pytest-args &apos;-W ignore::DeprecationWarning&apos;
      </pre>

      <div className="callout">
        <strong>The local backend is not a sandbox.</strong> The model&apos;s shell commands run as
        you, on your machine, with your network and your filesystem. There is a refusal list for the
        catastrophic-and-never-needed commands and a hard timeout on every command, but that narrows
        the blast radius rather than removing it. Point it at repositories you already trust, and
        use Docker for anything else.
      </div>

      <div className="rule">
        <span>@@ with docker @@</span>
        <b>the isolated path</b>
        <span>one throwaway container per task</span>
      </div>

      <pre className="shell-block">
        <span className="p">$</span> docker build -f sandbox/Dockerfile.sandbox -t swe-agent-sandbox:latest sandbox/
        {"\n"}
        <span className="p">$</span> WORKSPACE_BACKEND=docker python -m eval.record_run --instance pallets__flask-4992
        {"\n\n"}
        <span className="c"># or the whole local stack — API, Jaeger, Prometheus</span>
        {"\n"}
        <span className="p">$</span> docker-compose up
      </pre>

      <div className="rule">
        <span>@@ the full benchmark @@</span>
        <b>300 instances</b>
        <span>bring budget</span>
      </div>

      <pre className="shell-block">
        <span className="p">$</span> pip install -e &quot;.[eval]&quot;{"   "}
        <span className="c"># the SWE-bench dataset</span>
        {"\n"}
        <span className="p">$</span> python -m eval.run_eval --compare --limit 10
        {"\n"}
        <span className="p">$</span> python -m eval.run_eval --approach agent --limit 50 --provider openai
      </pre>

      <dl className="facts" style={{ marginTop: 28 }}>
        <div className="fact">
          <dt>
            What it costs
            <InfoTip
              label="the cost of running it"
              text="The one instance measured here cost $0.11 on Sonnet 5. A --compare --limit 10 run is twenty of those, so budget a few dollars; the full 300-instance comparison is a different order of magnitude."
            />
          </dt>
          <dd className="small">$0.11 / issue</dd>
        </div>
        <div className="fact">
          <dt>
            Prerequisites
            <InfoTip
              label="what you need installed"
              text="Python 3.11 or newer, git, bash, and one provider key. Docker only if you want the isolated backend. PyTorch is an optional extra — search falls back to BM25 without it."
            />
          </dt>
          <dd className="small">Python 3.11+</dd>
        </div>
        <div className="fact">
          <dt>
            Tests
            <InfoTip
              label="the test suite"
              text="Fully mocked — no Docker, no network, no model calls — so they run in under a second. Coverage is partial and the README says exactly which parts are and are not covered."
            />
          </dt>
          <dd>85</dd>
        </div>
        <div className="fact">
          <dt>
            Source
            <InfoTip
              label="the repository"
              text="Everything on this page — the agent, both architectures, the harness, the recorder and this site — is in one public repository."
            />
          </dt>
          <dd className="small">
            <a className="link" href={REPO} target="_blank" rel="noreferrer">
              GitHub ↗
            </a>
          </dd>
        </div>
      </dl>
    </>
  );
}
