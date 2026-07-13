import { Hero } from "@/components/Hero";
import { Benchmark } from "@/components/Benchmark";
import { DemoConsole } from "@/components/DemoConsole";
import { Architecture } from "@/components/Architecture";

const REPO = "https://github.com/shiva-shivanibokka/Autonomous-SWE-Agent";

export default function Page() {
  return (
    <>
      <nav className="nav">
        <div className="wrap nav-inner">
          <span className="brand">
            <span className="brand-dot" />
            swe-agent
          </span>
          <div className="nav-links">
            <a href="#benchmark">Benchmark</a>
            <a href="#demo">Console</a>
            <a href="#architecture">Architecture</a>
            <a href={REPO} target="_blank" rel="noreferrer">Source ↗</a>
          </div>
        </div>
      </nav>

      <main>
        <Hero />
        <Benchmark />
        <DemoConsole />
        <Architecture />
      </main>

      <footer className="footer">
        <div className="wrap footer-inner">
          <span>Autonomous SWE Agent — agentic vs. agentless on SWE-bench-lite.</span>
          <span>
            <a href={REPO} target="_blank" rel="noreferrer">GitHub</a> · Bring your own key ·
            No key stored
          </span>
        </div>
      </footer>
    </>
  );
}
