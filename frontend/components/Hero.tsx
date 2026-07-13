export function Hero() {
  return (
    <header className="hero wrap">
      <p className="eyebrow">Autonomous software engineering · SWE-bench-lite</p>
      <h1>
        Two ways to fix a bug.
        <br />
        <span className="tint-agent">One agent</span>,{" "}
        <span className="tint-agentless">one pipeline</span>,
        <br />
        300 real GitHub issues.
      </h1>
      <p className="lede">
        This system resolves genuine GitHub issues autonomously and runs the same benchmark two
        ways — a free-form <span className="tint-agent">agentic tool-use loop</span> and a
        deterministic <span className="tint-agentless">3-phase agentless pipeline</span> — then
        accounts for every token, turn, and dollar. Grounded in Anthropic&apos;s SWE-bench work and
        the Agentless paper.
      </p>
      <div className="hero-meta">
        <span><span className="tick">✓</span> Docker-isolated per task</span>
        <span><span className="tick">✓</span> Bring your own key — Anthropic · OpenAI · Google · Groq</span>
        <span><span className="tick">✓</span> OpenTelemetry + Prometheus</span>
      </div>

      <div className="rail">
        <div className="rail-card agent">
          <div className="rail-tag">Approach A · Agentic</div>
          <h3>Tool-use loop</h3>
          <p>
            One model drives a free-form loop with three tools until it decides the job is done.
            More capable on hard issues; costs more per fix.
          </p>
          <div className="rail-flow">issue → [ bash · editor · search ] × N turns → &lt;DONE&gt;</div>
        </div>
        <div className="rail-card agentless">
          <div className="rail-tag">Approach B · Agentless</div>
          <h3>Localize → repair → validate</h3>
          <p>
            No tools. Localize the fault, sample patch candidates, and pick the winner by running
            the tests. Cheaper and deterministic.
          </p>
          <div className="rail-flow">localize → repair ×10 → validate → best patch</div>
        </div>
      </div>
    </header>
  );
}
