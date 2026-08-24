export function Hero() {
  return (
    <header className="hero wrap">
      <p className="eyebrow">Autonomous software engineering · SWE-bench-lite</p>
      <h1>
        Two ways to fix a bug.
        <br />
        One agent, <em>one pipeline</em>.
      </h1>
      <p className="lede">
        This system takes a real GitHub issue, writes a patch inside an isolated workspace, and runs
        the repository&apos;s own tests against it — built two ways, a free-form agentic tool-use
        loop and a deterministic three-phase pipeline that never touches a tool, so the same task
        can be run through both and compared with every token, turn and dollar accounted for.
      </p>
      <div className="hero-meta">
        <span>
          <span className="tick">✓</span> Docker-isolated per task — or no Docker at all
        </span>
        <span>
          <span className="tick">✓</span> Bring your own key: Anthropic · OpenAI · Google · Groq
        </span>
        <span>
          <span className="tick">✓</span> OpenTelemetry traces + Prometheus metrics
        </span>
      </div>
    </header>
  );
}
