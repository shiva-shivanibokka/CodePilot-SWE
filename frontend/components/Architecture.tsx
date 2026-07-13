export function Architecture() {
  return (
    <section className="section wrap" id="architecture">
      <p className="eyebrow">How it works</p>
      <h2>Same 300 issues, two architectures</h2>
      <div className="arch">
        <div className="arch-col a">
          <h3><span className="swatch" /> Agentic loop</h3>
          <ol className="arch-steps">
            <li>
              <strong>Explore &amp; search</strong>
              <p>BM25 + embedding search over the repo finds suspect files before reading them.</p>
            </li>
            <li>
              <strong>Reproduce</strong>
              <p>The agent writes a script that triggers the bug and confirms it in a sandbox.</p>
            </li>
            <li>
              <strong>Edit with str_replace</strong>
              <p>Surgical edits require a unique match, preventing the classic overwrite mistake.</p>
            </li>
            <li>
              <strong>Verify &amp; stop</strong>
              <p>Runs the test suite, then emits &lt;DONE&gt;. A context-budget manager compresses history near the limit.</p>
            </li>
          </ol>
        </div>
        <div className="arch-col b">
          <h3><span className="swatch" /> Agentless pipeline</h3>
          <ol className="arch-steps">
            <li>
              <strong>Localize</strong>
              <p>A repo map (file tree + signatures) lets the model name suspect files and functions — no tools.</p>
            </li>
            <li>
              <strong>Repair ×10</strong>
              <p>Ten candidate patches are sampled at temperature, giving diverse fixes to choose from.</p>
            </li>
            <li>
              <strong>Validate</strong>
              <p>Each patch is applied and tested in the sandbox; the winner is chosen by pass rate, not by the model.</p>
            </li>
            <li>
              <strong>Submit best</strong>
              <p>The highest-scoring patch is kept. Cheaper and fully deterministic in selection.</p>
            </li>
          </ol>
        </div>
      </div>
    </section>
  );
}
